import { promises as fs } from 'fs'
import path from 'path'

// Preview route: serves the SAME editor.html the Telegram bot serves,
// so what you see here is exactly what the bot's «План карта» link opens.
export async function GET() {
  const editorPath = path.join(
    process.cwd(),
    'bot',
    'addons',
    'plan_map',
    'editor.html',
  )
  const html = await fs.readFile(editorPath, 'utf-8')
  return new Response(html, {
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  })
}
