# Third-party notices (XENIO UI)

## Ship Notes "speaking-orb" — MIT License

XENIO's orb (`src/orb/renderer.ts`) adapts the particle technique of Ship Notes' speaking orb: a Fibonacci sphere of
point sprites drawn with WebGL, shapes blended by weights, a radial halo, and loudness from a Web Audio analyser.
Source: https://github.com/aqualang89/shipnotes-components (`components/speaking-orb/speaking-orb.js`).

```
MIT License

Copyright (c) 2026 Ship Notes

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

XENIO's own additions (the sixteen states, the lattice, constellation and scan shapes, the palette, the GSAP-driven
transitions and the state machine) are part of XENIO.

## Libraries

| Package | Version | License |
|---|---|---|
| react, react-dom | 19.3.1 | MIT |
| gsap | 3.15.0 | GSAP Standard "no charge" license (https://gsap.com/standard-license) |
| lucide-react | 1.59.0 | ISC |

Build and test tools (vite, typescript, vitest, testing-library, jsdom) are development dependencies only and are not
part of the built interface.
