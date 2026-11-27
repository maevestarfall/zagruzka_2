import { useState } from 'react';
import JSZip from 'jszip';
import { Download, Loader2 } from 'lucide-react';

// ── Root config files ──────────────────────────────────────────────────────
import serverMjs from '../../../server.mjs?raw';
import botMjs from '../../../bot.mjs?raw';
import renderYaml from '../../../render.yaml?raw';
import dockerfile from '../../../Dockerfile?raw';
import envExample from '../../../env.example?raw';
import packageJson from '../../../package.json?raw';
import gitignore from '../../../.gitignore?raw';
import dockerignore from '../../../.dockerignore?raw';
import gitattributes from '../../../.gitattributes?raw';
import npmrc from '../../../.npmrc?raw';
import viteConfig from '../../../vite.config.ts?raw';
import postcssConfig from '../../../postcss.config.mjs?raw';
import pnpmWorkspace from '../../../pnpm-workspace.yaml?raw';
import pnpmLock from '../../../pnpm-lock.yaml?raw';
import defaultShadcnTheme from '../../../default_shadcn_theme.css?raw';

// ── bot-data ───────────────────────────────────────────────────────────────
import servicesJson from '../../../bot-data/services.json?raw';

// ── styles ─────────────────────────────────────────────────────────────────
import stylesFonts from '../../styles/fonts.css?raw';
import stylesGlobals from '../../styles/globals.css?raw';
import stylesIndex from '../../styles/index.css?raw';
import stylesTailwind from '../../styles/tailwind.css?raw';
import stylesTheme from '../../styles/theme.css?raw';

// ── App ────────────────────────────────────────────────────────────────────
import appTsx from '../App.tsx?raw';

// ── Custom components ──────────────────────────────────────────────────────
import typesTsx from './types.ts?raw';
import dataTsx from './data.ts?raw';
import sidebarTsx from './Sidebar.tsx?raw';
import toolCardTsx from './ToolCard.tsx?raw';
import verificationLogTsx from './VerificationLog.tsx?raw';
import apiPanelTsx from './ApiPanel.tsx?raw';
import useApiTs from './useApi.ts?raw';
import botViewTsx from './BotView.tsx?raw';
import telegramChatTsx from './TelegramChat.tsx?raw';
import workflowPanelTsx from './WorkflowPanel.tsx?raw';
import workflowTypesTsx from './workflowTypes.ts?raw';
import downloadZipTsx from './DownloadZip.tsx?raw';
import fileZoneTsx from './FileZone.tsx?raw';
import imageWithFallbackTsx from './figma/ImageWithFallback.tsx?raw';

// ── UI components (shadcn) ─────────────────────────────────────────────────
import uiAccordion from './ui/accordion.tsx?raw';
import uiAlertDialog from './ui/alert-dialog.tsx?raw';
import uiAlert from './ui/alert.tsx?raw';
import uiAspectRatio from './ui/aspect-ratio.tsx?raw';
import uiAvatar from './ui/avatar.tsx?raw';
import uiBadge from './ui/badge.tsx?raw';
import uiBreadcrumb from './ui/breadcrumb.tsx?raw';
import uiButton from './ui/button.tsx?raw';
import uiCalendar from './ui/calendar.tsx?raw';
import uiCard from './ui/card.tsx?raw';
import uiCarousel from './ui/carousel.tsx?raw';
import uiChart from './ui/chart.tsx?raw';
import uiCheckbox from './ui/checkbox.tsx?raw';
import uiCollapsible from './ui/collapsible.tsx?raw';
import uiCommand from './ui/command.tsx?raw';
import uiContextMenu from './ui/context-menu.tsx?raw';
import uiDialog from './ui/dialog.tsx?raw';
import uiDrawer from './ui/drawer.tsx?raw';
import uiDropdownMenu from './ui/dropdown-menu.tsx?raw';
import uiForm from './ui/form.tsx?raw';
import uiHoverCard from './ui/hover-card.tsx?raw';
import uiInputOtp from './ui/input-otp.tsx?raw';
import uiInput from './ui/input.tsx?raw';
import uiLabel from './ui/label.tsx?raw';
import uiMenubar from './ui/menubar.tsx?raw';
import uiNavigationMenu from './ui/navigation-menu.tsx?raw';
import uiPagination from './ui/pagination.tsx?raw';
import uiPopover from './ui/popover.tsx?raw';
import uiProgress from './ui/progress.tsx?raw';
import uiRadioGroup from './ui/radio-group.tsx?raw';
import uiResizable from './ui/resizable.tsx?raw';
import uiScrollArea from './ui/scroll-area.tsx?raw';
import uiSelect from './ui/select.tsx?raw';
import uiSeparator from './ui/separator.tsx?raw';
import uiSheet from './ui/sheet.tsx?raw';
import uiSidebar from './ui/sidebar.tsx?raw';
import uiSkeleton from './ui/skeleton.tsx?raw';
import uiSlider from './ui/slider.tsx?raw';
import uiSonner from './ui/sonner.tsx?raw';
import uiSwitch from './ui/switch.tsx?raw';
import uiTable from './ui/table.tsx?raw';
import uiTabs from './ui/tabs.tsx?raw';
import uiTextarea from './ui/textarea.tsx?raw';
import uiToggleGroup from './ui/toggle-group.tsx?raw';
import uiToggle from './ui/toggle.tsx?raw';
import uiTooltip from './ui/tooltip.tsx?raw';
import uiUseMobile from './ui/use-mobile.ts?raw';
import uiUtils from './ui/utils.ts?raw';

// ─────────────────────────────────────────────────────────────────────────────

const FILES: Record<string, string> = {
  // Root
  'server.mjs': serverMjs,
  'bot.mjs': botMjs,
  'render.yaml': renderYaml,
  'Dockerfile': dockerfile,
  'env.example': envExample,
  'package.json': packageJson,
  '.gitignore': gitignore,
  '.dockerignore': dockerignore,
  '.gitattributes': gitattributes,
  '.npmrc': npmrc,
  'vite.config.ts': viteConfig,
  'postcss.config.mjs': postcssConfig,
  'pnpm-workspace.yaml': pnpmWorkspace,
  'pnpm-lock.yaml': pnpmLock,
  'default_shadcn_theme.css': defaultShadcnTheme,
  // bot-data
  'bot-data/services.json': servicesJson,
  // styles
  'src/styles/fonts.css': stylesFonts,
  'src/styles/globals.css': stylesGlobals,
  'src/styles/index.css': stylesIndex,
  'src/styles/tailwind.css': stylesTailwind,
  'src/styles/theme.css': stylesTheme,
  // App
  'src/app/App.tsx': appTsx,
  // Custom components
  'src/app/components/types.ts': typesTsx,
  'src/app/components/data.ts': dataTsx,
  'src/app/components/Sidebar.tsx': sidebarTsx,
  'src/app/components/ToolCard.tsx': toolCardTsx,
  'src/app/components/VerificationLog.tsx': verificationLogTsx,
  'src/app/components/ApiPanel.tsx': apiPanelTsx,
  'src/app/components/useApi.ts': useApiTs,
  'src/app/components/BotView.tsx': botViewTsx,
  'src/app/components/TelegramChat.tsx': telegramChatTsx,
  'src/app/components/WorkflowPanel.tsx': workflowPanelTsx,
  'src/app/components/workflowTypes.ts': workflowTypesTsx,
  'src/app/components/DownloadZip.tsx': downloadZipTsx,
  'src/app/components/FileZone.tsx': fileZoneTsx,
  'src/app/components/figma/ImageWithFallback.tsx': imageWithFallbackTsx,
  // UI components
  'src/app/components/ui/accordion.tsx': uiAccordion,
  'src/app/components/ui/alert-dialog.tsx': uiAlertDialog,
  'src/app/components/ui/alert.tsx': uiAlert,
  'src/app/components/ui/aspect-ratio.tsx': uiAspectRatio,
  'src/app/components/ui/avatar.tsx': uiAvatar,
  'src/app/components/ui/badge.tsx': uiBadge,
  'src/app/components/ui/breadcrumb.tsx': uiBreadcrumb,
  'src/app/components/ui/button.tsx': uiButton,
  'src/app/components/ui/calendar.tsx': uiCalendar,
  'src/app/components/ui/card.tsx': uiCard,
  'src/app/components/ui/carousel.tsx': uiCarousel,
  'src/app/components/ui/chart.tsx': uiChart,
  'src/app/components/ui/checkbox.tsx': uiCheckbox,
  'src/app/components/ui/collapsible.tsx': uiCollapsible,
  'src/app/components/ui/command.tsx': uiCommand,
  'src/app/components/ui/context-menu.tsx': uiContextMenu,
  'src/app/components/ui/dialog.tsx': uiDialog,
  'src/app/components/ui/drawer.tsx': uiDrawer,
  'src/app/components/ui/dropdown-menu.tsx': uiDropdownMenu,
  'src/app/components/ui/form.tsx': uiForm,
  'src/app/components/ui/hover-card.tsx': uiHoverCard,
  'src/app/components/ui/input-otp.tsx': uiInputOtp,
  'src/app/components/ui/input.tsx': uiInput,
  'src/app/components/ui/label.tsx': uiLabel,
  'src/app/components/ui/menubar.tsx': uiMenubar,
  'src/app/components/ui/navigation-menu.tsx': uiNavigationMenu,
  'src/app/components/ui/pagination.tsx': uiPagination,
  'src/app/components/ui/popover.tsx': uiPopover,
  'src/app/components/ui/progress.tsx': uiProgress,
  'src/app/components/ui/radio-group.tsx': uiRadioGroup,
  'src/app/components/ui/resizable.tsx': uiResizable,
  'src/app/components/ui/scroll-area.tsx': uiScrollArea,
  'src/app/components/ui/select.tsx': uiSelect,
  'src/app/components/ui/separator.tsx': uiSeparator,
  'src/app/components/ui/sheet.tsx': uiSheet,
  'src/app/components/ui/sidebar.tsx': uiSidebar,
  'src/app/components/ui/skeleton.tsx': uiSkeleton,
  'src/app/components/ui/slider.tsx': uiSlider,
  'src/app/components/ui/sonner.tsx': uiSonner,
  'src/app/components/ui/switch.tsx': uiSwitch,
  'src/app/components/ui/table.tsx': uiTable,
  'src/app/components/ui/tabs.tsx': uiTabs,
  'src/app/components/ui/textarea.tsx': uiTextarea,
  'src/app/components/ui/toggle-group.tsx': uiToggleGroup,
  'src/app/components/ui/toggle.tsx': uiToggle,
  'src/app/components/ui/tooltip.tsx': uiTooltip,
  'src/app/components/ui/use-mobile.ts': uiUseMobile,
  'src/app/components/ui/utils.ts': uiUtils,
};

const README = `# AI Tool Inspector

Система верификации инструментов ИИ-агентов с Telegram ботом.

## Как работает связь с ИИ

ИИ-агент сам приходит к тебе — API-ключ НЕ нужен:

1. Сгенерируй токен: POST /api/tokens/:modelId
2. Дай ИИ probe URL: GET /api/probe/:modelId?token=xxx
3. ИИ забирает задания и твои сообщения
4. ИИ верифицирует: POST /api/probe/:modelId/verify?token=xxx
5. Ты пишешь ИИ: POST /api/messages/:modelId
6. ИИ отвечает: POST /api/probe/:modelId/reply?token=xxx

## Быстрый старт

\`\`\`bash
pnpm install
cp env.example .env
# Минимум: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
# Опционально для прямого чата: ANTHROPIC_API_KEY / OPENAI_API_KEY

node server.mjs   # API сервер на :3001
node bot.mjs      # Telegram бот (другой терминал)
\`\`\`

## API
- GET  /api/health                             — статус сервера
- GET  /api/models                             — все модели
- POST /api/tokens/:modelId                    — создать токен для ИИ
- GET  /api/probe/:modelId?token=              — ИИ забирает задания + сообщения
- POST /api/probe/:modelId/verify?token=       — ИИ верифицирует инструмент
- POST /api/probe/:modelId/reply?token=        — ИИ отвечает на сообщение
- POST /api/messages/:modelId                  — написать сообщение для ИИ
- GET  /api/messages/:modelId                  — история переписки
- POST /api/chat                               — прямой чат через AI API (нужен ключ)
- GET  /api/summary                            — сводка с trustScore
- GET  /docs                                   — Swagger UI

## Деплой на Render.com
1. Залей на GitHub
2. render.com → New → Blueprint → выбери репо
3. Dashboard → Environment → TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

## Деплой Docker (Oracle Always Free / VPS)
\`\`\`bash
docker build -t ai-tool-inspector .
docker run -p 3001:3001 \\
  -e TELEGRAM_BOT_TOKEN=xxx \\
  -e TELEGRAM_CHAT_ID=xxx \\
  -e SERVICE_URL=https://your-domain.com \\
  ai-tool-inspector
\`\`\`

## Telegram бот
/start, /stats, /services, /addserver, /link, /models, /tokens
`;

export async function triggerZipDownload(): Promise<void> {
  const zip = new JSZip();
  zip.file('README.md', README);
  for (const [filePath, content] of Object.entries(FILES)) {
    zip.file(filePath, content);
  }
  const blob = await zip.generateAsync({ type: 'blob', compression: 'DEFLATE' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'ai-tool-inspector.zip';
  a.click();
  URL.revokeObjectURL(url);
}

export function DownloadZipButton({ className }: { className?: string }) {
  const [status, setStatus] = useState<'idle' | 'loading' | 'done'>('idle');

  const handleDownload = async () => {
    setStatus('loading');
    try {
      await triggerZipDownload();
      setStatus('done');
      setTimeout(() => setStatus('idle'), 3000);
    } catch (e) {
      console.error(e);
      setStatus('idle');
    }
  };

  return (
    <button onClick={handleDownload} disabled={status === 'loading'} className={className}>
      {status === 'loading'
        ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
        : <Download className="w-3.5 h-3.5" />}
      {status === 'loading' ? 'Создаю...' : status === 'done' ? '✓ Скачано!' : 'Скачать ZIP'}
    </button>
  );
}
