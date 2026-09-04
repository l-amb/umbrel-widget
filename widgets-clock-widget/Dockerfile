FROM oven/bun:1 AS build
WORKDIR /app
COPY package.json ./
RUN bun install
COPY index.ts ./
RUN bun build --compile ./index.ts --outfile server

FROM gcr.io/distroless/base
COPY --from=build /app/server /server
EXPOSE 3000
CMD ["/server"]
