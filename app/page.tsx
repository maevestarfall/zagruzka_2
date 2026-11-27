import { redirect } from 'next/navigation'

// Открываем сразу редактор «План карта» (тот же editor.html, что отдаёт бот).
export default function Page() {
  redirect('/planmap/preview')
}
