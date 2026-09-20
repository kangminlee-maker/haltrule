/**
 * Everything the host gives the modules in this directory, and it is one
 * function. ts/tsconfig.json compiles them with no host types at all
 * ("types": []), so `process`, `console`, timers, `fetch`, `Buffer`, `require`
 * and every `node:` module do not exist for them: naming one is a compile
 * error, not something a scan has to find. SHA-256 is the dependency the spec
 * allows where the language has none of its own, and this is all of it.
 */
declare module "node:crypto" {
  export function createHash(algorithm: "sha256"): {
    update(data: string, inputEncoding: "utf8"): { digest(encoding: "hex"): string };
  };
}
