// Khronos glTF validator (npm gltf-validator) on one or more GLB files -> JSON summary per file.
const fs = require("fs"); const path = require("path");
const validator = require(path.join(__dirname, "node_modules", "gltf-validator"));
(async () => {
  const out = {};
  for (const f of process.argv.slice(2)) {
    const r = await validator.validateBytes(new Uint8Array(fs.readFileSync(f)), { uri: path.basename(f), maxIssues: 50 });
    out[path.basename(f)] = { errors: r.issues.numErrors, warnings: r.issues.numWarnings, infos: r.issues.numInfos, hints: r.issues.numHints,
      messages: r.issues.messages.filter(m => m.severity <= 1).map(m => `${m.code}: ${m.message} @ ${m.pointer}`),
      info: { version: r.info.version, generator: r.info.generator, drawCalls: r.info.drawCallCount, tris: r.info.totalTriangleCount,
              vertices: r.info.totalVertexCount, materials: r.info.materialCount, textures: r.info.textureCount, extensions: r.info.extensionsUsed } };
  }
  console.log(JSON.stringify(out, null, 1));
})();
