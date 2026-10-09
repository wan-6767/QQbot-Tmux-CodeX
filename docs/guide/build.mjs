import { cp, mkdir, readdir, rm } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const target = resolve(root, '../../dist/guide');
const assets = ['lucide.min.js', 'LUCIDE_LICENSE', 'mark.png', 'workbench.png'];
const files = ['index.html', 'styles.css', 'app.js', ...assets.map(name => `assets/${name}`)];
await rm(target, { recursive: true, force: true });
await mkdir(resolve(target, 'assets'), { recursive: true });
for (const file of files) await cp(resolve(root, file), resolve(target, file));
console.log(`Built ${files.length} static files into dist/guide (${(await readdir(target)).join(', ')}).`);
