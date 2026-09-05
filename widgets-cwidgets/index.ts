import { mkdir } from "fs/promises"

const START = Date.now()
const DATA_DIR = "/data"
const STATE_FILE = `${DATA_DIR}/state.json`

type WidgetId = "clock" | "date" | "greeting" | "dayProgress" | "weekNumber" | "uptime"

const WIDGET_IDS: WidgetId[] = ["clock", "date", "greeting", "dayProgress", "weekNumber", "uptime"]

const WIDGET_META: Record<WidgetId, { name: string; description: string }> = {
  clock: { name: "Clock", description: "Live current time" },
  date: { name: "Date", description: "Today's day and date" },
  greeting: { name: "Greeting", description: "A friendly message based on the time of day" },
  dayProgress: { name: "Day Progress", description: "How much of today has gone by" },
  weekNumber: { name: "Week Number", description: "The current ISO week of the year" },
  uptime: { name: "Widget Uptime", description: "How long this widget server has been running" },
}

async function loadState(): Promise<Record<string, boolean>> {
  try {
    const file = Bun.file(STATE_FILE)
    if (await file.exists()) return await file.json()
  } catch {}
  return Object.fromEntries(WIDGET_IDS.map((id) => [id, true]))
}

async function saveState(state: Record<string, boolean>) {
  await mkdir(DATA_DIR, { recursive: true })
  await Bun.write(STATE_FILE, JSON.stringify(state, null, 2))
}

let state = await loadState()

function nowParts() {
  const d = new Date()
  const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
  const shortDate = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })
  const weekday = d.toLocaleDateString("en-US", { weekday: "long" })
  const longDate = d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })

  const hour = d.getHours()
  let greeting = "Good night"
  if (hour >= 5 && hour < 12) greeting = "Good morning"
  else if (hour >= 12 && hour < 17) greeting = "Good afternoon"
  else if (hour >= 17 && hour < 21) greeting = "Good evening"

  const startOfDay = new Date(d)
  startOfDay.setHours(0, 0, 0, 0)
  const dayPct = Math.round(((d.getTime() - startOfDay.getTime()) / 86400000) * 100)

  const target = new Date(d.valueOf())
  const dayNr = (d.getDay() + 6) % 7
  target.setDate(target.getDate() - dayNr + 3)
  const firstThursday = target.valueOf()
  target.setMonth(0, 1)
  if (target.getDay() !== 4) {
    target.setMonth(0, 1 + ((4 - target.getDay() + 7) % 7))
  }
  const weekNumber = 1 + Math.ceil((firstThursday - target.valueOf()) / 604800000)

  return { time, shortDate, weekday, longDate, greeting, dayPct, weekNumber }
}

function widgetPayload(id: WidgetId): any {
  const p = nowParts()
  switch (id) {
    case "clock":
      return { type: "text-with-buttons", refresh: "1s", link: "", title: "Current Time", text: p.time, subtext: p.shortDate }
    case "date":
      return { type: "text-with-buttons", refresh: "60s", link: "", title: "Today", text: p.weekday, subtext: p.longDate }
    case "greeting":
      return { type: "text-with-buttons", refresh: "60s", link: "", title: p.greeting, text: p.time, subtext: "Have a great one" }
    case "dayProgress":
      return { type: "text-with-buttons", refresh: "60s", link: "", title: "Day Progress", text: `${p.dayPct}%`, subtext: "of today gone" }
    case "weekNumber":
      return { type: "text-with-buttons", refresh: "3600s", link: "", title: "Week Number", text: `Week ${p.weekNumber}`, subtext: new Date().getFullYear().toString() }
    case "uptime": {
      const secs = Math.floor((Date.now() - START) / 1000)
      const h = Math.floor(secs / 3600)
      const m = Math.floor((secs % 3600) / 60)
      return { type: "text-with-buttons", refresh: "30s", link: "", title: "Widget Uptime", text: `${h}h ${m}m`, subtext: "since last restart" }
    }
  }
}

const server = Bun.serve({
  port: 3000,
  async fetch(req) {
    const url = new URL(req.url)

    const widgetMatch = url.pathname.match(/^\/widgets\/([a-zA-Z]+)$/)
    if (widgetMatch) {
      const id = widgetMatch[1] as WidgetId
      if (!WIDGET_IDS.includes(id)) return new Response("Not found", { status: 404 })
      if (!state[id]) {
        return Response.json({ type: "text-with-buttons", refresh: "60s", link: "", title: WIDGET_META[id].name, text: "Off", subtext: "Enable it in the cwidgets gallery" })
      }
      return Response.json(widgetPayload(id))
    }

    if (url.pathname === "/api/widgets" && req.method === "GET") {
      return Response.json(WIDGET_IDS.map((id) => ({ id, ...WIDGET_META[id], enabled: !!state[id], preview: widgetPayload(id) })))
    }

    if (url.pathname.match(/^\/api\/widgets\/[a-zA-Z]+\/toggle$/) && req.method === "POST") {
      const id = url.pathname.split("/")[3] as WidgetId
      if (!WIDGET_IDS.includes(id)) return new Response("Not found", { status: 404 })
      state[id] = !state[id]
      await saveState(state)
      return Response.json({ id, enabled: state[id] })
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      const file = Bun.file("/public/index.html")
      if (await file.exists()) return new Response(file, { headers: { "Content-Type": "text/html" } })
    }

    return new Response("Not found", { status: 404 })
  },
})

console.log(`Listening on port ${server.port}`)
