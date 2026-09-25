import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { normalizeLatexDelimiters } from "./markdown.ts";

test("converts legacy inline and standalone math delimiters", () => {
  const result = normalizeLatexDelimiters("Inline \\(x^2\\).\n\n\\[\ny=x^2\n\\]");
  assert.match(result, /Inline \$x\^2\$\./);
  assert.match(result, /\$\$\ny=x\^2\n\$\$/);
});

test("keeps inline and fenced code literal", () => {
  const source = "Inline `\\(x\\)`.\n\n~~~python\nexpr = r\"\\[x^2\\]\"\n~~~";
  assert.equal(normalizeLatexDelimiters(source), source);
});

test("keeps unmatched or escaped delimiters as text", () => {
  const source = "Open \\(x and escaped \\\\(x\\\\)";
  assert.equal(normalizeLatexDelimiters(source), source);
});

test("treats bracket math in prose and tables as inline", () => {
  const source = "Equation \\[x^2\\].\n\n| value |\n| --- |\n| \\[x^2\\] |";
  const result = normalizeLatexDelimiters(source);
  assert.match(result, /Equation \$x\^2\$\./);
  assert.match(result, /\| \$x\^2\$ \|/);
  assert.doesNotMatch(result, /\$\$\n/);
});

test("renders fenced code blocks with common language labels", () => {
  const languages = ["python", "javascript", "json", "bash"];
  const markdown = languages.map((language) => `\`\`\`${language}\nexample ${language}\n\`\`\``).join("\n\n");
  const html = renderToStaticMarkup(React.createElement(ReactMarkdown, {
    remarkPlugins: [remarkGfm, remarkMath],
    rehypePlugins: [[rehypeKatex, { throwOnError: false }]],
  }, markdown));

  for (const language of languages) assert.match(html, new RegExp(`class=\"language-${language}\"`));
  assert.equal((html.match(/<pre>/g) ?? []).length, languages.length);
});
