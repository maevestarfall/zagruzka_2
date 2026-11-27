import { promises as fs } from 'fs'
import path from 'path'

// Preview data endpoint — mirrors bot/addons/plan_map/server.py
// (GET/POST /planmap/<token>/data). Stores the plan in the same
// storage file layout the bot uses: data/storage/plan_map.json.
//
// Storage format v2: {version: 2, active, plans: [5 x {name, nodes, edges}]}
// plus mirrored top-level title/nodes/edges of the active plan for
// backwards compatibility. Old v1 files {title, nodes, edges} are
// migrated into plan slot 0 on read.
const PLAN_FILE = path.join(process.cwd(), 'data', 'storage', 'plan_map.json')

const PLAN_COUNT = 5

type PlanBlock = { t: 'text' | 'action' | 'prompt'; v: string }

// Личный шрифт квадратика (режим «только выбранным»):
// побеждает глобальные настройки панели шрифта.
type NodeFont = { family?: string; size?: number; bold?: boolean }

type PlanNode = {
  id: string
  kind: string
  name: string
  text: string
  // Квадратик-фото: data-URL картинки (уже сжатой редактором, если вкл).
  img?: string
  // Свободный поток блоков квадратика: текст / действия / промт
  // в любом порядке (новый формат; старые поля ниже зеркалируются).
  blocks?: PlanBlock[]
  font?: NodeFont
  actions: string
  actions_note: string
  prompt: string
  prompt_note: string
  show_actions: boolean
  show_prompt: boolean
  x: number
  y: number
  w: number
  h: number
  rows: number
  cols: number
  cells: Record<string, string>
}

type PlanEdge = {
  from: string
  to: string
  style: number
  color?: string
  from_side: string
}

// Линия фломастера: цвет, толщина, SVG-путь
type DrawStroke = { c: string; w: number; d: string }

type Plan = {
  name: string
  nodes: PlanNode[]
  edges: PlanEdge[]
  draw: DrawStroke[]
}

const SIDES = new Set(['t', 'r', 'b', 'l', ''])

// Названия шрифтов из панели редактора: системный + 5 популярных из Word.
const FONT_IDS = new Set([
  'system',
  'calibri',
  'arial',
  'times',
  'verdana',
  'georgia',
])

function emptyPlan(i: number): Plan {
  return { name: `план ${i + 1}`, nodes: [], edges: [], draw: [] }
}

function emptyStore() {
  const plans: Plan[] = []
  for (let i = 0; i < PLAN_COUNT; i++) plans.push(emptyPlan(i))
  return {
    version: 2,
    active: 0,
    plans,
    title: 'План карта',
    nodes: [] as PlanNode[],
    edges: [] as PlanEdge[],
    updated_at: 0,
  }
}

// Same validation rules as server.py normalize_plan().
function normalizeSinglePlan(raw: unknown, fallbackName: string): Plan | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const obj = raw as Record<string, unknown>
  const nodesIn = obj.nodes
  const edgesIn = obj.edges
  if (!Array.isArray(nodesIn) || !Array.isArray(edgesIn)) return null

  const nodes: PlanNode[] = []
  const ids = new Set<string>()
  for (const n of nodesIn.slice(0, 500)) {
    if (typeof n !== 'object' || n === null) return null
    const node = n as Record<string, unknown>
    const nid = String(node.id ?? '')
    if (!nid || ids.has(nid)) return null
    ids.add(nid)
    const x = Number(node.x ?? 0)
    const y = Number(node.y ?? 0)
    if (Number.isNaN(x) || Number.isNaN(y)) return null
    const kindRaw = String(node.kind ?? 'box')
    const kind =
      kindRaw === 'note' ? 'note' : kindRaw === 'table' ? 'table' : 'box'
    let w = Number(node.w ?? 0)
    if (Number.isNaN(w) || w <= 0) w = 0
    else w = Math.min(kind === 'table' ? 1400 : 800, Math.max(160, w))
    // Ручная высота (тянется за кружок в центре); 0 = авто.
    let h = Number(node.h ?? 0)
    if (Number.isNaN(h) || h <= 0) h = 0
    else h = Math.min(1600, Math.max(60, h))
    let rows = Math.trunc(Number(node.rows ?? 0))
    if (Number.isNaN(rows) || rows < 0) rows = 0
    rows = Math.min(30, rows)
    let cols = Math.trunc(Number(node.cols ?? 0))
    if (Number.isNaN(cols) || cols < 0) cols = 0
    cols = Math.min(12, cols)
    const cells: Record<string, string> = {}
    const cellsIn = node.cells
    if (cellsIn && typeof cellsIn === 'object' && !Array.isArray(cellsIn)) {
      for (const [k, v] of Object.entries(
        cellsIn as Record<string, unknown>,
      ).slice(0, 400)) {
        if (/^\d+_\d+$/.test(k)) cells[k] = String(v ?? '').slice(0, 2000)
      }
    }
    let blocks: PlanBlock[] | undefined
    if (Array.isArray(node.blocks)) {
      blocks = []
      for (const b of (node.blocks as unknown[]).slice(0, 100)) {
        if (typeof b !== 'object' || b === null) continue
        const bb = b as Record<string, unknown>
        const t: PlanBlock['t'] =
          bb.t === 'action' ? 'action' : bb.t === 'prompt' ? 'prompt' : 'text'
        blocks.push({ t, v: String(bb.v ?? '').slice(0, 5000) })
      }
    }
    // Личный шрифт квадратика: {family?, size?, bold?} — сохраняем, чтобы
    // режим «только выбранным» переживал перезагрузку страницы.
    let font: NodeFont | undefined
    if (node.font && typeof node.font === 'object' && !Array.isArray(node.font)) {
      const f = node.font as Record<string, unknown>
      const out: NodeFont = {}
      const fam = String(f.family ?? '')
      if (FONT_IDS.has(fam)) out.family = fam
      const size = Math.trunc(Number(f.size ?? 0))
      if (!Number.isNaN(size) && size >= 9 && size <= 32) out.size = size
      if (typeof f.bold === 'boolean') out.bold = f.bold
      if (Object.keys(out).length) font = out
    }
    nodes.push({
      id: nid,
      kind,
      name: String(node.name ?? '').slice(0, 200),
      text: String(node.text ?? '').slice(0, 5000),
      ...(blocks ? { blocks } : {}),
      ...(font ? { font } : {}),
      actions: String(node.actions ?? '').slice(0, 5000),
      actions_note: String(node.actions_note ?? '').slice(0, 5000),
      prompt: String(node.prompt ?? '').slice(0, 5000),
      prompt_note: String(node.prompt_note ?? '').slice(0, 5000),
      show_actions: Boolean(node.show_actions),
      show_prompt: Boolean(node.show_prompt),
      x,
      y,
      w,
      h,
      rows,
      cols,
      cells,
    })
  }

  const edges: PlanEdge[] = []
  const seen = new Set<string>()
  for (const e of edgesIn.slice(0, 2000)) {
    if (typeof e !== 'object' || e === null) return null
    const edge = e as Record<string, unknown>
    const src = String(edge.from ?? '')
    const dst = String(edge.to ?? '')
    if (!ids.has(src) || !ids.has(dst) || src === dst) continue
    const key = `${src}\u0000${dst}`
    if (seen.has(key)) continue
    seen.add(key)
    let style = Math.trunc(Number(edge.style ?? 1))
    if (Number.isNaN(style) || style < 1 || style > 5) style = 1
    let fromSide = String(edge.from_side ?? '')
    if (!SIDES.has(fromSide)) fromSide = ''
    // Цвет стрелки — hex, выбирается пользователем рядом со стрелками.
    const colorRaw = String(edge.color ?? '')
    const color = /^#[0-9a-fA-F]{6}$/.test(colorRaw) ? colorRaw : undefined
    edges.push({
      from: src,
      to: dst,
      style,
      ...(color ? { color } : {}),
      from_side: fromSide,
    })
  }

  // Линии фломастера: [{c, w, d}] — рисование поверх всей области.
  const draw: DrawStroke[] = []
  if (Array.isArray(obj.draw)) {
    for (const s of (obj.draw as unknown[]).slice(0, 500)) {
      if (typeof s !== 'object' || s === null) continue
      const stroke = s as Record<string, unknown>
      const c = String(stroke.c ?? '')
      if (!/^#[0-9a-fA-F]{6}$/.test(c)) continue
      let w = Number(stroke.w ?? 4)
      if (Number.isNaN(w)) w = 4
      w = Math.min(24, Math.max(2, w))
      const d = String(stroke.d ?? '').slice(0, 20000)
      if (!d || !/^[MLQCZmlqcz0-9 .,-]+$/.test(d)) continue
      draw.push({ c, w, d })
    }
  }

  const name =
    String((obj.name ?? obj.title ?? '') || '').slice(0, 200) || fallbackName
  return { name, nodes, edges, draw }
}

function normalizeStore(raw: unknown) {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const obj = raw as Record<string, unknown>

  const store = emptyStore()

  if (Array.isArray(obj.plans)) {
    // v2 store: up to 5 plan slots.
    for (let i = 0; i < PLAN_COUNT; i++) {
      const p = normalizeSinglePlan(obj.plans[i], `план ${i + 1}`)
      if (p) store.plans[i] = p
    }
    let active = Math.trunc(Number(obj.active ?? 0))
    if (Number.isNaN(active) || active < 0 || active >= PLAN_COUNT) active = 0
    store.active = active
  } else {
    // legacy v1: single plan {title, nodes, edges}
    const p = normalizeSinglePlan(obj, 'план 1')
    if (p === null) return null
    store.plans[0] = p
    store.active = 0
  }

  const activePlan = store.plans[store.active]
  store.title = activePlan.name || 'План карта'
  store.nodes = activePlan.nodes
  store.edges = activePlan.edges
  store.updated_at = Math.floor(Date.now() / 1000)
  return store
}

async function loadStore() {
  try {
    const text = await fs.readFile(PLAN_FILE, 'utf-8')
    const store = normalizeStore(JSON.parse(text))
    return store ?? emptyStore()
  } catch {
    return emptyStore()
  }
}

export async function GET() {
  const store = await loadStore()
  return Response.json(store, {
    headers: { 'Cache-Control': 'no-store' },
  })
}

export async function POST(request: Request) {
  let raw: unknown
  try {
    raw = await request.json()
  } catch {
    return Response.json({ error: 'bad json' }, { status: 400 })
  }
  const store = normalizeStore(raw)
  if (store === null) {
    return Response.json({ error: 'bad plan shape' }, { status: 400 })
  }
  try {
    await fs.mkdir(path.dirname(PLAN_FILE), { recursive: true })
    await fs.writeFile(PLAN_FILE, JSON.stringify(store, null, 2), 'utf-8')
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 })
  }
  return Response.json({
    ok: true,
    nodes: store.nodes.length,
    edges: store.edges.length,
  })
}
