import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  server: { fs: { allow: [fileURLToPath(new URL('../..', import.meta.url))] } },
});
