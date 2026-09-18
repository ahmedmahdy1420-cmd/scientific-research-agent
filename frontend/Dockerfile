# Development image: the Vite dev server with hot reload.
# A production deployment would `npm run build` and serve dist/ from CloudFront
# or nginx; see README > AWS architecture.
FROM node:22-alpine

WORKDIR /app
COPY package.json ./
RUN npm install

COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev"]
