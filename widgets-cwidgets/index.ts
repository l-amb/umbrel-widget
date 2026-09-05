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

  return { time, shortDate, weekday, longDate, greeting }
}

const widgets: Record<string, () => object> = {
  clock: () => {
    const { time, shortDate } = nowParts()
    return {
      type: "text-with-buttons",
      refresh: "1s",
      link: "",
      title: "Current Time",
      text: time,
      subtext: shortDate,
    }
  },
  date: () => {
    const { weekday, longDate } = nowParts()
    return {
      type: "text-with-buttons",
      refresh: "60s",
      link: "",
      title: "Today",
      text: weekday,
      subtext: longDate,
    }
  },
  greeting: () => {
    const { greeting, time } = nowParts()
    return {
      type: "text-with-buttons",
      refresh: "60s",
      link: "",
      title: greeting,
      text: time,
      subtext: "Have a great one",
    }
  },
}

const server = Bun.serve({
  port: 3000,
  async fetch(req) {
    const url = new URL(req.url)

    const match = url.pathname.match(/^\/widgets\/([a-z]+)$/)
    if (match && widgets[match[1]]) {
      return Response.json(widgets[match[1]]())
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      const file = Bun.file("/public/index.html")
      if (await file.exists()) {
        return new Response(file, { headers: { "Content-Type": "text/html" } })
      }
    }

    return new Response("Not found", { status: 404 })
  },
})

console.log(`Listening on port ${server.port}`)
