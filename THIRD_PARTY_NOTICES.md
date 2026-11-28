# Third-Party Notices

Agent-Hire is licensed under the GNU AGPL v3.0 only (see `LICENSE` and `NOTICE`). It uses the
following third-party components, each under its own license.

## Shipped in the install packages

| Component | Where | License |
|---|---|---|
| [Node.js](https://nodejs.org/) | `app/runtime/node` (runs the agent) | MIT (`app/runtime/node/LICENSE`) |
| [uv](https://github.com/astral-sh/uv) | `app/runtime/bin` (runs the careers scraper) | Apache-2.0 or MIT |
| [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | `app/models` (text embeddings) | Apache-2.0 |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | linked into the API server | MIT |
| [DirectML](https://www.nuget.org/packages/Microsoft.AI.DirectML) | Windows package, `app/bin/DirectML.dll` | Microsoft software license (`app/bin/DirectML-LICENSE.txt`) |
| [Preact](https://preactjs.com/) | web UI (`app/web/dist`) | MIT |
| [d3-sankey](https://github.com/d3/d3-sankey), d3-array, d3-shape, d3-path | web UI, application flow chart | BSD-3-Clause |
| [Inter](https://rsms.me/inter/) | web UI font | SIL Open Font License 1.1 |
| [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | web UI font | SIL Open Font License 1.1 |

## Downloaded by the installer

The job level and department classifiers (`qilun-ai/job-level-classifier`,
`qilun-ai/job-department-classifier` on Hugging Face) are distributed separately under the
Agent-Hire Model License shown on their model pages.

## Libraries

Rust crates (`apps/api-rs/Cargo.lock`), npm packages (`apps/web`, `apps/agent`, `apps/extension`) and Python
packages (`services/careers-scraper/uv.lock`) are used under their own licenses, recorded in those
lockfiles and in each package.
