import fs from "node:fs/promises";
import path from "node:path";

import { validateDeck } from "./deck_spec_validation.mjs";


const value = process.argv[2] || process.env.DECK_SPEC_PATH;
if (!value) throw new Error("用法: node validate_spec.mjs <deck_spec.json>");
const specPath = path.resolve(value);
const spec = JSON.parse(await fs.readFile(specPath, "utf8"));
validateDeck(spec);
process.stdout.write(`${JSON.stringify({ valid: true, path: specPath, slides: spec.slides.length })}\n`);
