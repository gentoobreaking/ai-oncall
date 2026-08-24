# oncall-gate — Go 管線層（multi-stage，最終映像僅單一 binary + ca-certs）
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY gate/go.mod gate/go.sum ./
RUN go mod download
COPY gate/ .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/gate ./cmd/gate

FROM alpine:latest
RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -g 10000 oncall-gate && adduser -D -H -u 10000 -G oncall-gate oncall-gate
COPY --from=build /out/gate /usr/local/bin/gate
USER oncall-gate
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/gate"]
