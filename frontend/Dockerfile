# syntax=docker/dockerfile:1

FROM node:22-alpine AS build

WORKDIR /app
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html

# The reverse-proxy configuration is added in the Nginx/Compose step.
EXPOSE 80
