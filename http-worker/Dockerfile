FROM golang:1.22-alpine AS builder

WORKDIR /app

# Copy go.mod and go.sum first — Docker caches this layer separately.
# Dependencies are only re-downloaded when these files change, not on every code edit.
COPY go.mod go.sum ./
RUN go mod download

# Copy source and build the binary.
# The entry point is cmd/worker/main.go (not the root).
COPY . .
RUN go build -o worker ./cmd/worker

FROM alpine:3.19
RUN apk add --no-cache ca-certificates && \
    adduser -S -u 1000 appuser
WORKDIR /app
COPY --from=builder /app/worker .
USER appuser
CMD ["./worker"]
