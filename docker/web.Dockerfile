FROM node:22-alpine AS build
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM nginx:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 3000
