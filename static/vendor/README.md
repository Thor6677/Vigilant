# Vendored third-party scripts

Pages load these from `/static/vendor/`, never from a CDN: the CSP allows only
`'self'` plus the page nonce, and a CDN response has no Subresource Integrity,
so a compromised CDN would run as first-party code. `tests/test_vendored_scripts.py`
fails if a template loads an external script or a file here changes.

Each file below was taken from the package's npm tarball after checking the
tarball against the registry's `dist.integrity` (sha512). Filenames carry the
version, so they need no cache-busting.

| File | Package | Version | Path in tarball | npm integrity of the tarball | sha256 of the file |
|---|---|---|---|---|---|
| `htmx-1.9.12.min.js` | `htmx.org` | 1.9.12 | `package/dist/htmx.min.js` | `sha512-VZAohXyF7xPGS52IM8d1T1283y+X4D+Owf3qY1NZ9RuBypyu9l8cGsxUMAG5fEAb/DhT7rDoJ9Hpu5/HxFD3cw==` | `449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452` |
| `chart-4.4.0.umd.js` | `chart.js` | 4.4.0 | `package/dist/chart.umd.js` | `sha512-vQEj6d+z0dcsKLlQvbKIMYFHd3t8W/7L2vfJIbYcfyPcRx92CsHqECpueN8qVGNlKyDcr5wBrYAYKnfu/9Q1hQ==` | `321e3a3fa98da4aaa957d10be57cbb514de0989eed8f9d726b5d05902cd01904` |
| `chartjs-adapter-date-fns-3.0.0.bundle.min.js` | `chartjs-adapter-date-fns` | 3.0.0 | `package/dist/chartjs-adapter-date-fns.bundle.min.js` | `sha512-Rs3iEB3Q5pJ973J93OBTpnP7qoGwvq3nUnoMdtxO+9aoJof7UFcRbWcIDteXuYd1fgAvct/32T9qaLyLuZVwCg==` | `ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615` |
| `sortablejs-1.15.3.min.js` | `sortablejs` | 1.15.3 | `package/Sortable.min.js` | `sha512-zdK3/kwwAK1cJgy1rwl1YtNTbRmc8qW/+vgXf75A7NHag5of4pyI6uK86ktmQETyWRH7IGaE73uZOOBcGxgqZg==` | `72aa2c4f9f7cb2b8b3268052d6d2daa9d952f209b7fc5cc247ff9e1153db1f16` |

Notes:

- htmx was previously loaded from the bare `unpkg.com/htmx.org@1.9.12` URL,
  which resolves to `dist/htmx.min.js`; the vendored file is byte-identical.
- Chart.js 4.4.0 does not ship a `chart.umd.min.js`; the CDN generated one
  from `dist/chart.umd.js`, which is already minified, so it returned that file
  with a comment banner prepended. The vendored file is the package's own,
  without the banner.
- The adapter and SortableJS files are byte-identical to what the CDN served.

## Updating one

1. Look up `https://registry.npmjs.org/<package>/<version>` and note
   `dist.tarball` and `dist.integrity`.
2. Download the tarball and check that `sha512-` plus the base64 of its raw
   SHA-512 digest equals `dist.integrity`. Do not use it otherwise.
3. Extract the file from the path above, save it here under a new versioned
   name, delete the old one, and update the `<script>` tags in `app/templates/`.
4. Update this table and the manifest in `tests/test_vendored_scripts.py`.
