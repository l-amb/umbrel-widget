const server = Bun.serve({
  port: 3000,
  fetch(req) {
    const url = new URL(req.url)

    if (url.pathname === "/widgets/clock") {
      const now = new Date()
      const time = now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
      const date = now.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })

      return Response.json({
        type: "text-with-buttons",
        refresh: "1s",
        link: "",
        title: "Current Time",
        text: time,
        subtext: date,
      })
    }

    return new Response("Not found", { status: 404 })
  },
})

console.log(`Listening on port ${server.port}`)
