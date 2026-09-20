# 发布部署平台 · 前端 Dockerfile（多阶段：Node 构建 → Nginx 托管）
# 构建：docker build -t deploy-platform-frontend -f Dockerfile .
#
# 官方 nginx 镜像会 include conf.d；本仓库用完整 nginx.conf 覆盖主配置。

FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --registry=https://registry.npmmirror.com
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 80
