# The Bianque web pages, served by nginx, which also proxies the API on the same origin.
FROM node:24.21.0-alpine3.24 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.30.5-alpine
# The nginx image renders /etc/nginx/templates/*.template with the environment.
ENV BIANQUE_BACKEND_URL=http://backend:8000
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
