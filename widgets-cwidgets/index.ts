import { mkdir } from "fs/promises"

const START = Date.now()
const DATA_DIR = "/data"
const STATE_FILE = `${DATA_DIR}/state.json`

const SLOT_IDS = ["slot1", "slot2", "slot3"] as const
type SlotId = typeof SLOT_IDS[number]

type WidgetType = "clock" | "date" | "greeting" | "dayProgress" | "weekNumber" | "uptime"
const WIDGET_TYPES: WidgetType[] = ["clock", "date", "greeting", "dayProgress", "weekNumber", "uptime"]

const TYPE_META: Record<WidgetType, { name: string; description: string; accent: string }> = {
  clock: { name: "🕐 Clock", description: "Live current time", accent: "linear-gradient(135deg,#f7b733,#fc4a1a)" },
  date: { name: "📅 Date", description: "Today's day and date", accent: "linear-gradient(135deg,#4facfe,#00f2fe)" },
  greeting: { name: "👋 Greeting", description: "A friendly message based on the time of day", accent: "linear-gradient(135deg,#ff9a9e,#fad0c4)" },
  dayProgress: { name: "📊 Day Progress", description: "How much of today has gone by", accent: "linear-gradient(135deg,#43cea2,#185a9d)" },
  weekNumber: { name: "🗓️ Week Number", description: "The current ISO week of the year", accent: "linear-gradient(135deg,#8e2de2,#4a00e0)" },
  uptime: { name: "⏱️ Widget Uptime", description: "How long this widget server has been running", accent: "linear-gradient(135deg,#232526,#414345)" },
}

interface SettingDef {
  key: string
  label: string
  type: "boolean" | "select" | "text"
  default: boolean | string
  options?: { value: string; label: string }[]
}

const TYPE_SETTINGS: Partial<Record<WidgetType, SettingDef[]>> = {
  clock: [
    { key: "showSeconds", label: "Show seconds", type: "boolean", default: true },
    { key: "use24Hour", label: "Use 24-hour time", type: "boolean", default: false },
    { key: "showDate", label: "Show date", type: "boolean", default: true },
  ],
  date: [
    {
      key: "format", label: "Format", type: "select", default: "short",
      options: [
        { value: "short", label: "Short (Sat, Sep 5)" },
        { value: "long", label: "Long (Saturday, September 5, 2026)" },
      ],
    },
  ],
  greeting: [
    { key: "name", label: "Your name (optional)", type: "text", default: "" },
  ],
  dayProgress: [
    {
      key: "style", label: "Style", type: "select", default: "percent",
      options: [
        { value: "percent", label: "Percentage" },
        { value: "bar", label: "Progress bar" },
      ],
    },
  ],
  weekNumber: [
    { key: "showYear", label: "Show year", type: "boolean", default: true },
  ],
  uptime: [
    { key: "compact", label: "Compact format (4h 12m)", type: "boolean", default: true },
  ],
}

type SettingsValues = Record<string, boolean | string>

interface SlotState {
  type: WidgetType
  settings: SettingsValues
}

interface State {
  slots: Record<SlotId, SlotState>
}

function defaultSettingsFor(type: WidgetType): SettingsValues {
  const defs = TYPE_SETTINGS[type]
  return defs ? Object.fromEntries(defs.map((d) => [d.key, d.default])) : {}
}

function defaultSlotState(type: WidgetType): SlotState {
  return { type, settings: defaultSettingsFor(type) }
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
    slots[id] = { type, settings: { ...defaultSettingsFor(type), ...(loadedSlot?.settings || {}) } }
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
  const isoDate = d.toISOString().slice(0, 10)

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

  return { d, shortDate, weekday, longDate, isoDate, greeting, dayPct, weekNumber }
}

function formatClockTime(d: Date, settings: SettingsValues) {
  return d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: settings.showSeconds === false ? undefined : "2-digit",
    hour12: settings.use24Hour ? false : true,
  })
}

function greetingEmoji(g: string) {
  if (g === "Good morning") return "🌅"
  if (g === "Good afternoon") return "🌞"
  if (g === "Good evening") return "🌆"
  return "🌙"
}

function renderType(type: WidgetType, settings: SettingsValues): any {
  const p = nowParts()
  switch (type) {
    case "clock":
      return {
        type: "text-with-buttons",
        refresh: settings.showSeconds === false ? "60s" : "1s",
        link: "",
        title: "🕐 Current Time",
        text: formatClockTime(p.d, settings),
        subtext: settings.showDate === false ? "" : p.shortDate,
      }
    case "date": {
      const isLong = settings.format === "long"
      return {
        type: "text-with-buttons",
        refresh: "60s",
        link: "",
        title: "📅 Today",
        text: isLong ? p.longDate : p.weekday,
        subtext: isLong ? p.isoDate : p.longDate,
      }
    }
    case "greeting": {
      const timeStr = formatClockTime(p.d, { showSeconds: false, use24Hour: false })
      const name = typeof settings.name === "string" ? settings.name.trim() : ""
      return {
        type: "text-with-buttons",
        refresh: "60s",
        link: "",
        title: `${greetingEmoji(p.greeting)} ${p.greeting}${name ? `, ${name}` : ""}`,
        text: timeStr,
        subtext: "Have a great one",
      }
    }
    case "dayProgress": {
      if (settings.style === "bar") {
        const filled = Math.round(p.dayPct / 10)
        const bar = "▓".repeat(filled) + "░".repeat(10 - filled)
        return { type: "text-with-buttons", refresh: "60s", link: "", title: "📊 Day Progress", text: bar, subtext: `${p.dayPct}% of today gone` }
      }
      return { type: "text-with-buttons", refresh: "60s", link: "", title: "📊 Day Progress", text: `${p.dayPct}%`, subtext: "of today gone" }
    }
    case "weekNumber":
      return {
        type: "text-with-buttons",
        refresh: "3600s",
        link: "",
        title: "🗓️ Week Number",
        text: `Week ${p.weekNumber}`,
        subtext: settings.showYear === false ? "" : p.d.getFullYear().toString(),
      }
    case "uptime": {
      const secs = Math.floor((Date.now() - START) / 1000)
      const h = Math.floor(secs / 3600)
      const m = Math.floor((secs % 3600) / 60)
      const text = settings.compact === false ? `${h} hours ${m} minutes` : `${h}h ${m}m`
      return { type: "text-with-buttons", refresh: "30s", link: "", title: "⏱️ Widget Uptime", text, subtext: "since last restart" }
    }
  }
}

function slotListPayload() {
  return SLOT_IDS.map((id) => {
    const slot = state.slots[id]
    return {
      id,
      type: slot.type,
      accent: TYPE_META[slot.type].accent,
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
        if (def.key in body) {
          slot.settings[def.key] = def.type === "boolean" ? !!body[def.key] : String(body[def.key])
        }
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
