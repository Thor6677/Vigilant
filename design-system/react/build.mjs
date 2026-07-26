import * as esbuild from 'esbuild';

await esbuild.build({
  entryPoints: ['src/index.ts'],
  outfile: 'dist/index.js',
  bundle: true,
  format: 'esm',
  jsx: 'automatic',
  external: ['react', 'react-dom', 'react/jsx-runtime'],
  loader: { '.woff2': 'file' },
  logLevel: 'info',
});

await esbuild.build({
  entryPoints: ['src/styles.css'],
  outfile: 'dist/index.css',
  bundle: true,
  loader: { '.woff2': 'file' },
  logLevel: 'info',
});
