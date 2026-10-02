# Parking Guardian frontend.
# Stage 1 builds the React app. Stage 2 is nginx serving the built files and
# passing /api/ requests on to the backend, so the browser talks to one address only.

# --- Stage 1: build ---
FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY public ./public
COPY src ./src

# Create React App reads this at build time. A relative path means "same address as the page".
ARG REACT_APP_API_BASE=/api/v1
ENV REACT_APP_API_BASE=$REACT_APP_API_BASE
RUN npm run build

# --- Stage 2: serve ---
# The unprivileged image runs as a normal user and listens on 8080.
FROM nginxinc/nginx-unprivileged:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html
EXPOSE 8080
