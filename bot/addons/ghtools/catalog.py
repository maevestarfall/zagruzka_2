"""Catalog of GitHub tools for video/motion/dynamics analysis.

Each tool has:
* ``key``      — stable id used in callback_data;
* ``emoji`` / ``name`` / ``repo`` — display + link;
* ``purpose``  — one-liner what it's good for;
* ``runnable`` — ``True`` if we can actually run it on the host (CPU, pip),
                 ``False`` if it needs GPU / heavy model → clone + show recipe;
* ``runner``   — key into :mod:`runner` (only for runnable tools);
* ``scenario`` — the *алгоритм* (numbered sequence of actions) shown on tap;
* ``note``     — caveat (optional).
"""

from __future__ import annotations

TOOLS: list[dict] = [
    {
        "key": "opticalflow",
        "emoji": "🌀",
        "name": "OpenCV Optical Flow (Farneback)",
        "repo": "https://github.com/hzwer/Awesome-Optical-Flow",
        "purpose": "Реальное движение по пикселям — динамика ракурса/экшна.",
        "runnable": True,
        "runner": "opticalflow",
        "builtin": True,
        "scenario": [
            "Скачать видео во временный файл.",
            "Нарезать кадры с шагом проверки (cv2).",
            "Между соседними кадрами посчитать dense optical flow "
            "(calcOpticalFlowFarneback) → магнитуда смещения в пикселях.",
            "Взять 95-й перцентиль магнитуды (ловит и локальное движение).",
            "Вердикт: высокий flow = реальная динамика; ~0 = статика/смаз.",
        ],
        "note": "Уже встроено в «🎞 Анализ динамики видео» как основная метрика.",
    },
    {
        "key": "pyscenedetect",
        "emoji": "✂️",
        "name": "PySceneDetect",
        "repo": "https://github.com/Breakthrough/PySceneDetect",
        "purpose": "Точные смены сцен/кадров (shot boundaries).",
        "runnable": True,
        "runner": "pyscenedetect",
        "scenario": [
            "Скачать видео.",
            "Прогнать ContentDetector (порог по изменению контента).",
            "Получить список сцен: начало/конец каждой по таймкоду.",
            "Показать, где реально режется действие — нарезку можно вести "
            "по сценам, а не по фиксированным секундам.",
        ],
    },
    {
        "key": "blur",
        "emoji": "🌫",
        "name": "Blur Detection (variance-of-Laplacian)",
        "repo": "https://github.com/topics/blur-detection",
        "purpose": "Какие кадры смазаны/нерезкие.",
        "runnable": True,
        "runner": "blur",
        "scenario": [
            "Скачать видео и нарезать кадры.",
            "Для каждого кадра посчитать variance of Laplacian (резкость).",
            "Ниже порога → кадр смазан; пометить такие кадры с таймкодами.",
            "Полезно вместе с optical flow: смаз + нулевой flow = ложная динамика.",
        ],
    },
    {
        "key": "videoanalyzer",
        "emoji": "🧠",
        "name": "byjlw/video-analyzer",
        "repo": "https://github.com/byjlw/video-analyzer",
        "purpose": "Кадры → Vision-LLM (Llama 3.2) + Whisper → описание видео.",
        "runnable": False,
        "scenario": [
            "git clone репозитория.",
            "pip install . (нужны ollama + Llama 3.2 Vision локально, либо "
            "ключ OpenRouter).",
            "video-analyzer video.mp4 → вытаскивает ключевые кадры, "
            "описывает каждый с учётом предыдущих, добавляет транскрипт.",
            "На выходе output/analysis.json: покадровый разбор + итоговое "
            "описание видео.",
        ],
        "note": "Тяжёлый VLM — на Render-хосте без GPU не потянет. Клон + "
        "команды покажу; гонять лучше отдельно или через OpenRouter-ключ.",
    },
    {
        "key": "llmvideoframes",
        "emoji": "🎞",
        "name": "simonw/llm-video-frames",
        "repo": "https://github.com/simonw/llm-video-frames",
        "purpose": "Видео → JPEG-кадры (fps + таймкоды) в любой Vision-LLM.",
        "runnable": False,
        "scenario": [
            "llm install llm-video-frames (нужен ffmpeg + CLI `llm`).",
            "llm -f 'video-frames:video.mp4?fps=2&timestamps=1' 'опиши ключевые "
            "сцены' -m gpt-4.1-mini.",
            "Плагин режет видео на кадры с таймкодами и кормит в модель.",
        ],
        "note": "Нужен ffmpeg и внешний LLM-CLI. Покажу команды.",
    },
    {
        "key": "transnetv2",
        "emoji": "🎬",
        "name": "soCzech/TransNetV2",
        "repo": "https://github.com/soCzech/TransNetV2",
        "purpose": "SOTA shot-boundary detection (deep, F1 BBC 96).",
        "runnable": False,
        "scenario": [
            "git clone + скачать веса модели.",
            "pip install tensorflow (или torch-порт).",
            "Прогнать инференс на видео → точные границы кадров.",
        ],
        "note": "Нужен GPU/TF. На хосте не запустится — даю клон + рецепт.",
    },
    {
        "key": "raft",
        "emoji": "🏞",
        "name": "princeton-vl/RAFT",
        "repo": "https://github.com/princeton-vl/RAFT",
        "purpose": "SOTA deep optical flow (точнейшее движение/ракурс).",
        "runnable": False,
        "scenario": [
            "git clone + скачать чекпоинты (models/).",
            "pip install torch torchvision.",
            "python demo.py --model=raft-things.pth --path=frames/ → плотный "
            "flow между кадрами высокой точности.",
        ],
        "note": "Нужен GPU. Для лёгкого варианта используй встроенный Farneback.",
    },
    {
        "key": "autoshot",
        "emoji": "🎯",
        "name": "wentaozhu/AutoShot",
        "repo": "https://github.com/wentaozhu/AutoShot",
        "purpose": "Deep shot-boundary detection (точнее на части датасетов).",
        "runnable": False,
        "scenario": [
            "git clone + веса.",
            "pip install зависимостей (torch).",
            "Инференс на видео → границы кадров.",
        ],
        "note": "Тяжёлый. Клон + рецепт.",
    },
]

TOOLS_BY_KEY = {t["key"]: t for t in TOOLS}
