import { mkdir } from "fs/promises"

const START = Date.now()
const DATA_DIR = "/data"
const STATE_FILE = `${DATA_DIR}/state.json`

const SLOT_IDS = ["slot1", "slot2", "slot3"] as const
type SlotId = typeof SLOT_IDS[number]

type WidgetType = "clock" | "date" | "greeting" | "dayProgress" | "weekNumber" | "uptime"
const WIDGET_TYPES: WidgetType[] = ["clock", "date", "greeting", "dayProgress", "weekNumber", "uptime"]

const TYPE_META: Record<WidgetType, { name: string; description: string }> = {
  clock: { name: "Clock", description: "Live current time" },
  date: { name: "Date", description: "Today's day and date" },
  greeting: { name: "Greeting", description: "A friendly message based on the time of day" },
  dayProgress: { name: "Day Progress", description: "How much of today has gone by" },
  weekNumber: { name: "Week Number", description: "The current ISO week of the year" },
  uptime: { name: "Widget Uptime", description: "How long this widget server has been running" },
}

interface SettingDef {
  key: string
  label: string
  type: "boolean"
  default: boolean
}

const TYPE_SETTINGS: Partial<Record<WidgetType, SettingDef[]>> = {
  clock: [
    { key: "showSeconds", label: "Show seconds", type: "boolean", default: true },
    { key: "use24Hour", label: "Use 24-hour time", type: "boolean", default: false },
  ],
}

interface SlotState {
  type: WidgetType
  settings: Record<string, boolean>
}

interface State {
  slots: Record<SlotId, SlotState>
}

function defaultSlotState(type: WidgetType): SlotState {
  const defs = TYPE_SETTINGS[type]
  return { type, settings: defs ? Object.fromEntries(defs.map((d) => [d.key, d.default])) : {} }
}

function defaultState(): State {
  const defaults: WidgetType[] = ["clock", "date", "greeting"]
  return {
    slots: Object.fromEntries(SLOT_IDS.map((id, i) => [id, defaultSlotState(defaults[i])])) as Record<SlotId, SlotState>,
  }
}

function mergeWithDefaults(loaded: any): State {
  const base = defaultState()
  const slots: Record<string, SlotState> = {}
  for (const id of SLOT_IDS) {
    const loadedSlot = loaded?.slots?.[id]
    const type: WidgetType = WIDGET_TYPES.includes(loadedSlot?.type) ? loadedSlot.type : base.slots[id].type
    const defs = TYPE_SETTINGS[type]
    const defaultSettings = defs ? Object.fromEntries(defs.map((d) => [d.key, d.default])) : {}
    slots[id] = { type, settings: { ...defaultSettings, ...(loadedSlot?.settings || {}) } }
  }
  return { slots: slots as Record<SlotId, SlotState> }
}

async function loadState(): Promise<State> {
  try {
    const file = Bun.file(STATE_FILE)
    if (await file.exists()) return mergeWithDefaults(await file.json())
  } catch {}
  return defaultState()
}

async function saveState(state: State) {
  await mkdir(DATA_DIR, { recursive: true })
  await Bun.write(STATE_FILE, JSON.stringify(state, null, 2))
}

let state = await loadState()

function nowParts() {
  const d = new Date()
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

  return { d, shortDate, weekday, longDate, greeting, dayPct, weekNumber }
}

function formatClockTime(d: Date, settings: Record<string, boolean>) {
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: settings.showSeconds === false ? undefined : "2-digit",
    hour12: settings.use24Hour ? false : true,
  })
}

function renderType(type: WidgetType, settings: Record<string, boolean>): any {
  const p = nowParts()
  switch (type) {
    case "clock":
      return {
        type: "text-with-buttons",
        refresh: settings.showSeconds === false ? "60s" : "1s",
        link: "",
        title: "Current Time",
        text: formatClockTime(p.d, settings),
        subtext: p.shortDate,
      }
    case "date":
      return { type: "text-with-buttons", refresh: "60s", link: "", title: "Today", text: p.weekday, subtext: p.longDate }
    case "greeting":
      return {
        type: "text-with-buttons",
        refresh: "60s",
        link: "",
        title: p.greeting,
        text: formatClockTime(p.d, { showSeconds: false, use24Hour: false }),
        subtext: "Have a great one",
      }
    case "dayProgress":
      return { type: "text-with-buttons", refresh: "60s", link: "", title: "Day Progress", text: `${p.dayPct}%`, subtext: "of today gone" }
    case "weekNumber":
      return { type: "text-with-buttons", refresh: "3600s", link: "", title: "Week Number", text: `Week ${p.weekNumber}`, subtext: p.d.getFullYear().toString() }
    case "uptime": {
      const secs = Math.floor((Date.now() - START) / 1000)
      const h = Math.floor(secs / 3600)
      const m = Math.floor((secs % 3600) / 60)
      return { type: "text-with-buttons", refresh: "30s", link: "", title: "Widget Uptime", text: `${h}h ${m}m`, subtext: "since last restart" }
    }
  }
}

function slotListPayload() {
  return SLOT_IDS.map((id) => {
    const slot = state.slots[id]
    return {
      id,
      type: slot.type,
      settingsSchema: TYPE_SETTINGS[slot.type] || [],
      settings: slot.settings,
      preview: renderType(slot.type, slot.settings),
    }
  })
}

const server = Bun.serve({
  port: 3000,
  async fetch(req) {
    const url = new URL(req.url)

    const slotMatch = url.pathname.match(/^\/widgets\/(slot[123])$/)
    if (slotMatch) {
      const id = slotMatch[1] as SlotId
      const slot = state.slots[id]
      return Response.json(renderType(slot.type, slot.settings))
    }

    if (url.pathname === "/api/slots" && req.method === "GET") {
      return Response.json({
        slots: slotListPayload(),
        types: WIDGET_TYPES.map((t) => ({ id: t, ...TYPE_META[t], settingsSchema: TYPE_SETTINGS[t] || [] })),
      })
    }

    const typeMatch = url.pathname.match(/^\/api\/slots\/(slot[123])\/type$/)
    if (typeMatch && req.method === "POST") {
      const id = typeMatch[1] as SlotId
      const body = await req.json().catch(() => ({}))
      const newType = body.type as WidgetType
      if (!WIDGET_TYPES.includes(newType)) return new Response("Invalid type", { status: 400 })
      state.slots[id] = defaultSlotState(newType)
      await saveState(state)
      return Response.json({ id, type: newType })
    }

    const settingsMatch = url.pathname.match(/^\/api\/slots\/(slot[123])\/settings$/)
    if (settingsMatch && req.method === "POST") {
      const id = settingsMatch[1] as SlotId
      const slot = state.slots[id]
      const defs = TYPE_SETTINGS[slot.type]
      if (!defs) return new Response("No settings for this widget", { status: 400 })
      const body = await req.json().catch(() => ({}))
      for (const def of defs) {
        if (def.key in body) slot.settings[def.key] = !!body[def.key]
      }
      await saveState(state)
      return Response.json({ id, settings: slot.settings })
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      const file = Bun.file("/public/index.html")
      if (await file.exists()) return new Response(file, { headers: { "Content-Type": "text/html" } })
    }

    return new Response("Not found", { status: 404 })
  },
})

console.log(`Listening on port ${server.port}`)
