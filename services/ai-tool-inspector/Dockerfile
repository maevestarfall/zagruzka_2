FROM node:20-alpine

WORKDIR /app

# Install pnpm
RUN npm install -g pnpm

# Copy package files
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml* ./

# Install dependencies (production only)
RUN pnpm install --frozen-lockfile --prod

# Copy application files
COPY server.mjs bot.mjs ./
COPY bot-data ./bot-data

# Build React app if needed
COPY src ./src
COPY vite.config.ts postcss.config.mjs __figma__entrypoint__.ts ./
RUN pnpm run build || echo "⚠️ Build failed, continuing..."

# Create data directory for persistence
RUN mkdir -p /app/bot-data

EXPOSE 3001

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
  CMD node -e "require('http').get('http://localhost:3001/api/health', (r) => { process.exit(r.statusCode === 200 ? 0 : 1); });"

# Start server (автоматически запустит бота если TELEGRAM_BOT_TOKEN установлен)
CMD ["node", "server.mjs"]
