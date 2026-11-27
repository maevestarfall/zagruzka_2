"""Built-in reference routes (ТЗ rev.7.1 §16.3).

The v0.app example ships as a preset in ``scn:menu`` («Пример: v0.app») and is
also used by the acceptance autotest (§16.4). Exact v0.app selectors may
drift over time — they are a reference, not a constant; the selector priority
of §16.2 is what matters.
"""

from __future__ import annotations

from .models import CODE6_REGEX, EMAIL_REGEX, Route, Stage, Step

V0_PRESET_ID = "preset-v0app"

# What the user says to the bot (§16.3) — used as planner input in tests.
V0_USER_TEXT = (
    "Зайди на сайт А (моя почта по ссылке {mail_url}), скопируй адрес почты. "
    "Открой v0.app во второй вкладке, нажми \"Sign Up\", в поле "
    "\"Email Address\" вставь почту, нажми \"Continue with Email\". "
    "Вернись на почту, дождись письма с кодом из 6 цифр, скопируй код. "
    "Вернись на v0.app, вставь код в поле кода и подтверди."
)


def build_v0_route(*, mail_url: str = "{mail_url}",
                   mail_selector: str = ".mail-address",
                   letter_selector: str = ".letter-body") -> Route:
    """Эталонный маршрут §16.3: регистрация v0.app (почта + код).

    ``mail_url``/``mail_selector``/``letter_selector`` подставляются под
    конкретный почтовый сайт (в тестах — локальные страницы §16.4).
    """
    return Route(
        id=V0_PRESET_ID,
        title="Регистрация v0.app: почта + код",
        site_a=mail_url,
        site_b=None,   # вкладка B открывается goto-шагом стадии 3
        stages=[
            Stage(index=1, title="Зайти на сайт с почтой", steps=[
                Step(op="goto", page="A", target=mail_url,
                     note="перейти на сайт почты"),
            ]),
            Stage(index=2, title="Скопировать почту", steps=[
                Step(op="copy", page="A", target=mail_selector,
                     save_as="email", extract=EMAIL_REGEX,
                     note="скопировать адрес почты"),
            ]),
            Stage(index=3, title="Открыть v0.app во второй вкладке (§15.1)", steps=[
                Step(op="goto", page="B", target="https://v0.app",
                     note="открыть v0.app во второй вкладке"),
                Step(op="click", page="B", target="text=Sign Up",
                     note="нажать кнопку «Sign Up»"),
            ]),
            Stage(index=4, title="Вставить почту и отправить", steps=[
                Step(op="type", page="B",
                     target='input[aria-label="Email Address"]',
                     value="{email}",
                     note="вставить почту в поле «Email Address»"),
                Step(op="click", page="B",
                     target='button:has-text("Continue with Email")',
                     note="нажать кнопку «Continue with Email»"),
            ]),
            Stage(index=5, title="Дождаться и скопировать код с почты (§15.5)", steps=[
                Step(op="copy", page="A", target=letter_selector,
                     save_as="code", extract=CODE6_REGEX, poll=True,
                     note="дождаться письма и скопировать код из 6 цифр"),
            ]),
            Stage(index=6, title="Вставить код и подтвердить", steps=[
                Step(op="type", page="B",
                     target='input[autocomplete="one-time-code"]',
                     value="{code}",
                     note="вставить код в поле кода"),
                Step(op="click", page="B", target='button[type="submit"]',
                     note="нажать кнопку подтверждения"),
            ]),
        ],
        vars={},
    )
