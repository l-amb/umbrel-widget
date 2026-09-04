FROM oven/bun:1 AS build
WORKDIR /app
COPY package.json ./
RUN bun install
COPY index.ts ./
ARG TARGETPLATFORM
RUN case "$TARGETPLATFORM" in \
      "linux/amd64") BUN_TARGET="bun-linux-x64" ;; \
      "linux/arm64") BUN_TARGET="bun-linux-arm64" ;; \
      *) echo "Unsupported platform: $TARGETPLATFORM" && exit 1 ;; \
    esac && \
    bun build --compile --target=$BUN_TARGET ./index.ts --outfile server

FROM gcr.io/distroless/base
COPY --from=build /app/server /server
EXPOSE 3000
CMD ["/server"]
