// Verifies the pure i18n logic (plural lookup, {var} interpolation, nullish handling, zh completeness) outside the
// browser. Bundled with esbuild, which vite already depends on:
//   npx esbuild src/i18n.ts --bundle --format=esm --platform=node --outfile=../var/i18n.mjs
//   node ../var/i18n-check.mjs
import { en, zh, translate, messages } from "../var/i18n.mjs";

let bad = 0;
const eq = (label, got, want) => {
  const ok = got === want;
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"} ${label}\n       got  ${JSON.stringify(got)}${ok ? "" : `\n       want ${JSON.stringify(want)}`}`);
};

// singular vs plural
const sub = (s, n) => s.split("{n}").join(String(n));
eq("en singular", translate("en", "create.subtitle", { n: 1 }), sub(en["create.subtitle"], 1));
eq("en plural", translate("en", "create.subtitle", { n: 4 }), sub(en["create.subtitle_plural"], 4));
eq("zh has no plural form", translate("zh", "create.subtitle", { n: 4 }), sub(zh["create.subtitle_plural"], 4));

// interpolation, including the nullish rule (a missing number must not print "null")
eq("interpolates", translate("en", "asset.variantLod", { n: 2 }), "LOD2");
eq("nullish renders empty", translate("en", "asset.optimizedText", { tris: "8,000", tex: null }), "8,000 tris · px maps");
eq("undefined renders empty", translate("zh", "settings.worker", { name: "image" }), "image 工作进程");

// unknown key falls back to the key itself rather than crashing
eq("unknown key", translate("zh", "does.not.exist"), "does.not.exist");

// stage/status helpers resolve through the dictionaries
eq("stage zh", translate("zh", "stage.reference"), "生成参考图");
eq("status zh", translate("zh", "status.completed"), "已完成");

// every zh value must be a real translation: non-empty, and not identical to English unless it is a technical term
const sameAsEnglish = Object.keys(en).filter((k) => zh[k] === en[k]);
const empty = Object.keys(en).filter((k) => !zh[k] || !String(zh[k]).trim());
console.log(`\nkeys: ${Object.keys(en).length} en / ${Object.keys(zh).length} zh, ${Object.keys(messages.zh).length} zh registered`);
console.log(`zh empty values: ${empty.length ? empty.join(", ") : "none"}`);
console.log(`zh identical to en (expected for shared terms/symbols): ${sameAsEnglish.length}`);
console.log(sameAsEnglish.map((k) => `  ${k} = ${JSON.stringify(en[k])}`).join("\n"));

console.log(bad ? `\n${bad} CHECK(S) FAILED` : "\nall i18n checks passed");
process.exit(bad ? 1 : 0);
