import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const allowedHosts = (env.DIRECTOR_ALLOWED_HOSTS || '')
    .split(',')
    .map((host) => host.trim())
    .filter(Boolean);
  const port = Number(env.DIRECTOR_WEB_PORT || 3000);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('DIRECTOR_WEB_PORT must be an integer between 1 and 65535');
  }

  return {
    plugins: [react()],
    server: {
      host: env.DIRECTOR_WEB_HOST || '127.0.0.1',
      port,
      ...(allowedHosts.length ? { allowedHosts } : {}),
      proxy: { '/v1': 'http://localhost:8000', '/healthz': 'http://localhost:8000' },
    },
  };
});
