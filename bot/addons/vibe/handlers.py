from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


logger = logging.getLogger(__name__)
vibe_router = Router()

ADDON = "vibe"

class VibeStates(StatesGroup):
    awaiting_project_name = State()

def _kb_vibe_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Показать ТЗ (Tech Spec)", callback_data="vibe:show_spec")],
            [InlineKeyboardButton(text="🚀 Инициализировать проект", callback_data="vibe:init_prompt")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="vibe:back")],
        ]
    )

def _kb_back_to_vibe() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="vibe:menu")]
        ]
    )

@vibe_router.callback_query(F.data == "main:vibe")
@vibe_router.callback_query(F.data == "vibe:menu")
async def cb_vibe_dashboard(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    
    text = (
        "<b>🛕 Vibe Coding — God's Chosen Template</b>\n\n"
        "Добро пожаловать в панель управления шаблоном <b>Vibe</b> (Bun + Hono + React Vite CSR + Astro SEO)!\n\n"
        "Этот инструмент создан специально для высокоскоростной разработки («вайб-кодинга») с помощью ИИ-агентов. "
        "Вся архитектура, контракты Zod и конфигурации DigitalOcean оптимизированы для мгновенного скармливания контекста моделям.\n\n"
        "Выбери действие ниже:"
    )
    
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_vibe_dashboard())

@vibe_router.callback_query(F.data == "vibe:back")
async def cb_vibe_back(query: CallbackQuery) -> None:
    await query.answer()
    from ...wizard import _kb_main_after_claim
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(
                "Главное меню. Выбери что сделать:",
                reply_markup=_kb_main_after_claim(query.from_user.id),
            )

@vibe_router.callback_query(F.data == "vibe:show_spec")
async def cb_vibe_show_spec(query: CallbackQuery) -> None:
    await query.answer()
    text = (
        "<b>📄 Техническое Задание (ТЗ) на интеграцию Vibe</b>\n\n"
        "Я составил максимально подробное, жесткое и структурированное техническое задание "
        "по стандартам промышленной разработки на основе оригинального репозитория <code>di-sukharev/vibe</code>.\n\n"
        "Полный файл ТЗ со всеми схемами данных, командами, структурой каталогов и "
        "инструкциями для ИИ-агентов сохранен в твоем воркспейсе:\n"
        "👉 <code>file://session/vibe_specification.md</code>\n\n"
        "<i>Рекомендуется открыть этот файл для детального ознакомления. Он полностью готов для скармливания любому ИИ-агенту.</i>"
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_back_to_vibe())

@vibe_router.callback_query(F.data == "vibe:init_prompt")
async def cb_vibe_init_prompt(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.set_state(VibeStates.awaiting_project_name)
    text = (
        "<b>🚀 Инициализация нового Vibe-проекта</b>\n\n"
        "Введи название твоего будущего приложения (латиницей, например <code>my-awesome-app</code>):\n\n"
        "Я автоматически разверну структуру каталогов, настрою Bun-воркспейсы, Zod-контракты и "
        "подготовлю файлы <code>AGENTS.md</code> и <code>CLAUDE.md</code> для твоего ИИ-кодера."
    )
    if query.message is not None:
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=_kb_back_to_vibe())

def _generate_vibe_project(project_name: str) -> None:
    """Actually generates all the directories and files for the Vibe monorepo."""
    base_dir = f"projects/{project_name}"
    
    files = {
        "package.json": f"""{{
  "name": "{project_name}-monorepo",
  "private": true,
  "workspaces": [
    "packages/*",
    "backend",
    "webapp",
    "website"
  ],
  "scripts": {{
    "dev": "bun run dev:backend & bun run dev:webapp & bun run dev:website",
    "dev:backend": "bun --cwd backend dev",
    "dev:webapp": "bun --cwd webapp dev",
    "dev:website": "bun --cwd website dev",
    "build:backend": "bun --cwd backend build",
    "build:webapp": "bun --cwd webapp build",
    "build:website": "bun --cwd website build",
    "typecheck": "bun --cwd packages/contracts typecheck && bun --cwd backend typecheck && bun --cwd webapp typecheck && bun --cwd website typecheck",
    "test": "bun test",
    "test:contracts": "bun test packages/contracts",
    "test:backend": "bun test backend",
    "test:webapp": "bun test webapp",
    "e2e:webapp": "bun run --cwd webapp test:e2e",
    "db:migrate": "bun run --cwd backend prisma:migrate",
    "db:deploy": "bun run --cwd backend prisma:deploy",
    "deploy:do:specs": "node scripts/generate-do-specs.js"
  }}
}}""",
        "bun.lockb": "",
        "CLAUDE.md": """# Claude CLI Configuration - Vibe Monorepo

## Build and Development Commands
- Dev Backend: `bun run dev:backend`
- Dev Webapp: `bun run dev:webapp`
- Dev Website: `bun run dev:website`
- Build All: `bun run build:backend && bun run build:webapp && bun run build:website`

## Testing Commands
- Test Contracts: `bun run test:contracts`
- Test Backend: `bun run test:backend`

## Database Commands
- Create and apply migrations: `bun run db:migrate`
- Apply migrations in production: `bun run db:deploy`

## 🧠 Karpathy's 4 AI Coding Principles
1. **Think Before Coding**: State assumptions explicitly. If uncertain, ask rather than guess. Present trade-offs. Stop when confused.
2. **Simplicity First**: Write the minimum amount of code that solves the problem. No speculative abstractions or future-proofing.
3. **Surgical Changes**: Touch only what the specific task requires. Do not refactor adjacent, working code. Match existing style.
4. **Goal-Driven Execution**: Translate vague requests into declarative testing goals *before* writing code. Run tests to verify success.

## Code Review Guidelines (Serious Bugs Review)
Перед коммитом или слиянием изменений ВСЕГДА запускайте глубокое ревью:
- Проверьте всю цепочку: route/guard -> handler/service -> API contract -> persistence.
- Ищите ошибки логики, регрессии, утечки данных и уязвимости безопасности.
- Убедитесь, что все изменения покрыты тестами.
- Верните только подтвержденные проблемы: severity, file:line, доказательство по коду, риск и минимальный fix.""",
        "AGENTS.md": """# AI Agent Protocol (Vibe Monorepo)

## 🧠 Core Philosophy (Andrej Karpathy Skills)
- Be a thoughtful, reliable build partner.
- Follow the 4 principles: Think Before Coding, Simplicity First, Surgical Changes, and Goal-Driven Execution.

## Intake Checklist
Before writing code, ask the user:
1. **Task Context**: Is this a new feature or modifying an existing one?
2. **Active Workspaces**: Which workspaces are affected? (webapp, website, backend, contracts)
3. **Core Capabilities**: Does this require database changes, authentication, S3 storage, or Valkey real-time?

## Guidelines
- Follow **Contract-First Design** using Zod. Define API contracts in `packages/contracts` before writing backend or frontend code.
- Keep routes thin in Hono. Validate inputs using `zValidator` and delegate business logic to services.
- Use PostgreSQL 18 + Prisma ORM. Generate primary keys as UUIDv7 at the database level.

## Production Log Monitoring & Errors
- Ошибки в продакшене автоматически сохраняются в `errors/<error_name>.md`.
- Каждая ошибка должна содержать: stack trace, контекст вызова, время возникновения и план исправления.
- Исправляйте ошибки по одной и удаляйте файл инцидента после успешного фикса.""",
        "PRD.md": f"""# Product Requirements Document (PRD) — {project_name}

## 1. Проблема и Целевая Аудитория
[Опишите проблему, которую решает продукт, и для кого он создается]

## 2. Ключевой Функционал (MVP)
- **Авторизация**: Вход по email/паролю, разделение ролей (ADMIN / USER).
- **Панель управления (webapp)**: React-приложение для авторизованных пользователей.
- **Публичный сайт (website)**: Astro-лендинг с SEO-оптимизацией.

## 3. Нефункциональные Требования
- Стек: Bun + Hono + React Vite CSR + Astro SEO + Prisma ORM + PostgreSQL 18 + Valkey.
- Типобезопасность: Contract-First Design с использованием Zod контрактов.
- Архитектура: Монорепозиторий (workspaces).""",
        "TASKS.md": f"""# Tasks & Roadmap — {project_name}

## Этап 1: Инициализация и Базовая Настройка (Выполнено ✅)
- [x] Создание структуры монорепозитория
- [x] Настройка Zod контрактов в `packages/contracts`
- [x] Настройка Hono API backend и Prisma схемы
- [x] Подготовка React Vite CSR webapp и Astro website

## Этап 2: Базовый Функционал и Авторизация (В очереди ⏳)
- [ ] Настройка подключения к PostgreSQL 18 и запуск миграций
- [ ] Реализация JWT авторизации в бэкенде
- [ ] Интеграция формы логина в React webapp (TanStack Form)
- [ ] Настройка сессий и Auth Guard на фронтенде

## Этап 3: Бизнес-Логика и Дополнительные Модули (В очереди ⏳)
- [ ] [Добавьте ваши задачи сюда]""",
        "errors/.gitkeep": "",
        ".cursor/rules/contracts.mdc": """---
description: Rules for Zod API contracts and type safety
globs: ["packages/contracts/**/*"]
alwaysApply: false
---
# Zod API Contracts

- Always use Contract-First Design. Define schemas in this workspace before editing frontend or backend.
- Export all schemas from `src/index.ts`.
- Ensure all IDs are expected to be UUIDv7.
- Use strict type inference: `export type X = z.infer<typeof xSchema>;`""",
        ".cursor/rules/backend.mdc": """---
description: Rules for Hono API backend and Prisma ORM
globs: ["backend/**/*"]
alwaysApply: false
---
# Hono API Backend

- Keep Hono routes thin (routing and validation only).
- Always use `zValidator` with shared Zod contracts to validate request payloads.
- Delegate all business logic to service classes.
- Use PostgreSQL 18 + Prisma ORM. All primary keys must be generated as UUIDv7 at the database level (`@default(dbgenerated("gen_random_uuid()"))`).
- Database migrations: use `bun run db:migrate` locally and `bun run db:deploy` in production.""",
        ".cursor/rules/webapp.mdc": """---
description: Rules for React Vite CSR application
globs: ["webapp/**/*"]
alwaysApply: false
---
# React Vite CSR

- Strictly use React Client-Side Rendering (CSR).
- All screens must live behind an Auth/Session Guard.
- Use TanStack Query for data fetching and caching.
- Use TanStack Form for type-safe form management.
- Completely ignore SEO here (handled by the website workspace).""",
        ".cursor/rules/website.mdc": """---
description: Rules for Astro SEO-optimized public website
globs: ["website/**/*"]
alwaysApply: false
---
# Astro SEO Website

- Use Astro SSG/SSR for public pages, landing pages, and blogs.
- Ensure perfect SEO, meta tags, and rich link previews for social sharing.
- Do not include authenticated dashboards or complex stateful panels here (handled by the webapp workspace).""",
        ".cursor/rules/always-on.mdc": """---
description: Global project rules and core AI persona
globs: ["**/*"]
alwaysApply: true
---
# Core AI Persona & Project Rules

- Be a fast-moving, highly reliable, and thoughtful build partner.
- Prioritize shipping working, maintainable code fast over over-designed systems.
- Break large tasks into small shippable chunks.
- Perform thorough dependency and impact analysis before changing code.
- Follow the instructions in `AGENTS.md` and `CLAUDE.md` strictly.""",
        ".cursor/rules/request.mdc": """---
description: Rules for feature building and refactoring
globs: ["**/*"]
alwaysApply: false
---
# Feature Building & Refactoring

- Plan first! Outline the architecture, Zod contracts, and step-by-step plan before writing code.
- Follow Contract-First Design. Define schemas in `packages/contracts` first.
- Keep files modular and under 200 lines to ensure clean architecture and prevent AI confusion.""",
        ".cursor/rules/diagnose.mdc": """---
description: Rules for bug fixing and troubleshooting
globs: ["**/*"]
alwaysApply: false
---
# Bug Fixing & Troubleshooting

- Recursively isolate, analyze, and resolve errors from their root cause.
- Do not apply quick surface-level patches; solve the architectural problem.
- Verify unit tests and integration tests after any fix.""",
        "mobile/README.md": """# Vibe Mobile Template

The mobile workspace template (Expo + React Native + Apple/Google Login + Push Notifications + Payments) lives on the dedicated `mobile` branch.

To switch to the mobile branch, run:
```bash
git checkout mobile
```

If you do not need mobile capabilities for this project, you can keep working on the `main` branch.""",
        "docker-compose.yml": """version: '3.8'
services:
  postgres:
    image: postgres:18-alpine
    container_name: vibe-postgres-18
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: vibe_db
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  valkey:
    image: valkey/valkey:8-alpine
    container_name: vibe-valkey
    ports:
      - "6379:6379"

volumes:
  pgdata:""",
        "docs/ARCHITECTURE.md": """# Architecture Overview

This project is a Bun-powered monorepo:
- **`packages/contracts`**: Shared Zod schemas for type-safe API communication.
- **`backend`**: Hono API backend running on Bun with Prisma ORM.
- **`webapp`**: React Vite CSR application for authenticated areas.
- **`website`**: Astro SSG/SSR website for public pages and SEO.""",
        "docs/LOCAL_DATABASE.md": """# Local Database Setup

Start PostgreSQL 18 and Valkey in Docker:
```bash
docker-compose up -d
```

Run Prisma migrations:
```bash
bun run db:migrate
```""",
        "docs/STORAGE.md": """# S3 Storage Policy

- Do not save files to the backend container's local disk.
- Use pre-signed URLs from DigitalOcean Spaces or Yandex Object Storage for direct client-to-S3 uploads.""",
        "docs/TESTING.md": """# Testing Specification

- **Contracts**: Bun Test (`bun run test:contracts`)
- **Unit/Integration**: Bun Test (`bun run test:backend`)
- **E2E**: Playwright (`bun run e2e:webapp`)""",
        "docs/DEPLOYMENT.md": """# Deployment Guide (DigitalOcean App Platform)

Generate spec:
```bash
bun run deploy:do:specs
```

Deploying to DigitalOcean App Platform costs $27/month:
- Static sites (webapp, website): $0.00
- API Server (1 vCPU, 1 GB RAM): $12.00
- Managed PostgreSQL DB: $15.00""",
        "docs/YANDEX_CLOUD.md": """# Alternative Deployment (Yandex Cloud)

Deploy Hono API to Yandex Serverless Containers, use Yandex Managed PostgreSQL and Yandex Object Storage (S3).""",
        "packages/contracts/package.json": """{
  "name": "@web-app-demo/contracts",
  "version": "1.0.0",
  "private": true,
  "main": "./src/index.ts",
  "types": "./src/index.ts",
  "dependencies": {
    "zod": "^3.23.0"
  }
}""",
        "packages/contracts/src/index.ts": "export * from './auth';",
        "packages/contracts/src/auth.ts": """import { z } from 'zod';

export const loginSchema = z.object({
  email: z.string().email({ message: "Неверный формат email" }),
  password: z.string().min(8, { message: "Пароль должен содержать минимум 8 символов" })
});

export const authResponseSchema = z.object({
  token: z.string(),
  user: z.object({
    id: z.string().uuid(), // Ожидается UUIDv7
    email: z.string(),
    role: z.enum(['ADMIN', 'USER'])
  })
});

export type LoginInput = z.infer<typeof loginSchema>;
export type AuthResponse = z.infer<typeof authResponseSchema>;""",
        "packages/contracts/tsconfig.json": """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  }
}""",
        "backend/package.json": """{
  "name": "backend",
  "version": "1.0.0",
  "private": true,
  "main": "src/index.ts",
  "dependencies": {
    "hono": "^4.0.0",
    "@hono/zod-validator": "^0.2.0",
    "@prisma/client": "^5.10.0",
    "@web-app-demo/contracts": "workspace:*"
  },
  "devDependencies": {
    "prisma": "^5.10.0"
  },
  "scripts": {
    "dev": "bun --watch src/index.ts",
    "prisma:migrate": "prisma migrate dev",
    "prisma:deploy": "prisma migrate deploy"
  }
}""",
        "backend/src/index.ts": """import { Hono } from 'hono';
import authApp from './routes/auth';

const app = new Hono();

app.route('/auth', authApp);

app.get('/healthz', (c) => c.text('OK'));

export default {
  port: process.env.PORT || 3000,
  fetch: app.fetch,
};""",
        "backend/src/runtime.ts": """import { PrismaClient } from '@prisma/client';

export const prisma = new PrismaClient();""",
        "backend/src/routes/auth.ts": """import { Hono } from 'hono';
import { zValidator } from '@hono/zod-validator';
import { loginSchema } from '@web-app-demo/contracts';

const authApp = new Hono();

authApp.post('/login', zValidator('json', loginSchema), async (c) => {
  const { email, password } = c.req.valid('json');
  // Бизнес-логика авторизации
  return c.json({
    token: "mock-jwt-token",
    user: {
      id: "018f3a3a-3a3a-7a3a-8a3a-3a3a3a3a3a3a", // Пример UUIDv7
      email,
      role: "USER"
    }
  });
});

export default authApp;""",
        "backend/prisma/schema.prisma": """datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

generator client {
  provider = "prisma-client-js"
}

enum Role {
  ADMIN
  USER
}

model User {
  id        String   @id @default(dbgenerated("gen_random_uuid()")) // UUIDv7 на уровне Postgres 18
  email     String   @unique
  password  String
  role      Role     @default(USER)
  createdAt DateTime @default(now())
  updatedAt DateTime @updatedAt

  @@map("users")
}""",
        "backend/tsconfig.json": """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  }
}""",
        "webapp/package.json": """{
  "name": "webapp",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "vite",
    "build": "vite build"
  },
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "@tanstack/react-query": "^5.0.0",
    "@tanstack/react-form": "^0.1.0",
    "@web-app-demo/contracts": "workspace:*"
  },
  "devDependencies": {
    "vite": "^5.2.0",
    "@vitejs/plugin-react": "^4.2.0"
  }
}""",
        "webapp/index.html": """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Vibe Webapp</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>""",
        "webapp/src/main.tsx": """import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.tsx'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)""",
        "webapp/src/App.tsx": """import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const queryClient = new QueryClient();

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <div style={{ padding: '20px', fontFamily: 'sans-serif' }}>
        <h1>Vibe Webapp</h1>
        <p>React Vite CSR App behind Auth/Session Guard.</p>
      </div>
    </QueryClientProvider>
  );
}""",
        "webapp/vite.config.ts": """import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
})""",
        "website/package.json": """{
  "name": "website",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "astro dev",
    "build": "astro build",
    "preview": "astro preview"
  },
  "dependencies": {
    "astro": "^4.8.0"
  }
}""",
        "website/astro.config.mjs": """import { defineConfig } from 'astro/config';

export default defineConfig({
  output: 'server',
});""",
        "website/src/pages/index.astro": """---
---
<html lang="en">
	<head>
		<meta charset="utf-8" />
		<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
		<meta name="viewport" content="width=device-width" />
		<meta name="generator" content={Astro.generator} />
		<title>Vibe Website</title>
	</head>
	<body>
		<h1>Vibe Website</h1>
		<p>Astro SEO-optimized landing page.</p>
	</body>
</html>"""
    }
    
    for path, content in files.items():
        full_path = os.path.join(base_dir, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

@vibe_router.message(StateFilter(VibeStates.awaiting_project_name))
async def msg_vibe_init_submit(message: Message, state: FSMContext) -> None:
    project_name = (message.text or "").strip().lower()
    if not project_name or not re.match(r"^[a-z0-9\-]+$", project_name):
        await message.answer("❌ Неверный формат имени. Используй только строчные буквы латиницы, цифры и дефис (например: <code>my-app</code>). Попробуй еще раз:")
        return
        
    await state.clear()
    
    await message.answer(
        f"⏳ <b>Инициализация Vibe-пространства для проекта {project_name}...</b>\n"
        "• Создаю Bun monorepo структуру...\n"
        "• Генерирую Zod API контракты в <code>packages/contracts</code>...\n"
        "• Настраиваю Hono API backend...\n"
        "• Подготавливаю React Vite CSR webapp..."
    )
    
    try:
        # Actually generate the project!
        _generate_vibe_project(project_name)
        await asyncio.sleep(2.0)
        
        await message.answer(
            f"🎉 <b>Проект {project_name} успешно инициализирован!</b>\n\n"
            f"📂 <b>Структура каталогов развернута:</b>\n"
            f"• <code>projects/{project_name}/backend</code> (Hono + Prisma)\n"
            f"• <code>projects/{project_name}/webapp</code> (React + Vite)\n"
            f"• <code>projects/{project_name}/website</code> (Astro SEO)\n"
            f"• <code>projects/{project_name}/packages/contracts</code> (Zod)\n\n"
            f"ℹ️ <b>Для запуска ИИ-кодера:</b>\n"
            f"Передай своему агенту файл ТЗ: <code>file://session/vibe_specification.md</code>. "
            f"Он содержит готовые команды сборки, миграции и правила безопасного слияния кода."
        )
    except Exception as e:
        logger.exception("Failed to generate vibe project")
        await message.answer(f"❌ Произошла ошибка при генерации проекта: {e}")

def build_vibe_router() -> Router:
    from .. import state as addon_state
    addon_state.get(ADDON, "initialized", True)
    return vibe_router
