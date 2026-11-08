# 前端（Next.js）：构建期注入 NEXT_PUBLIC_API_BASE（浏览器侧 API 地址）
FROM node:20-alpine
RUN corepack enable
WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
ARG NEXT_PUBLIC_API_BASE=http://localhost:8100
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN pnpm build

EXPOSE 3000
CMD ["pnpm", "start"]
