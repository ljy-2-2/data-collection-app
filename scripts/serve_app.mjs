import { startProdServer } from '../node_modules/vinext/dist/server/prod-server.js';

await startProdServer({ host: process.argv[2] || '127.0.0.1', port: Number(process.argv[3] || 4173) });
