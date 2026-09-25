export function normalizeLatexDelimiters(markdown: string): string {
  function normalizeText(text: string): string {
    let result = "";
    for (let index = 0; index < text.length;) {
      if (text[index] === "`") {
        let length = 1;
        while (text[index + length] === "`") length += 1;
        const delimiter = "`".repeat(length);
        let end = text.indexOf(delimiter, index + length);
        while (end !== -1 && (text[end - 1] === "`" || text[end + length] === "`")) {
          end = text.indexOf(delimiter, end + length);
        }
        if (end !== -1) {
          result += text.slice(index, end + length);
          index = end + length;
          continue;
        }
      }

      const opening = text.startsWith("\\[", index) ? "\\[" : text.startsWith("\\(", index) ? "\\(" : "";
      if (opening) {
        let precedingSlashes = 0;
        for (let cursor = index - 1; cursor >= 0 && text[cursor] === "\\"; cursor -= 1) precedingSlashes += 1;
        if (precedingSlashes % 2 === 0) {
          const closing = opening === "\\[" ? "\\]" : "\\)";
          let end = text.indexOf(closing, index + 2);
          while (end !== -1) {
            let closingSlashes = 0;
            for (let cursor = end - 1; cursor >= 0 && text[cursor] === "\\"; cursor -= 1) closingSlashes += 1;
            if (closingSlashes % 2 === 0) break;
            end = text.indexOf(closing, end + closing.length);
          }
          const formula = end === -1 ? "" : text.slice(index + 2, end).trim();
          if (formula) {
            const lineStart = text.lastIndexOf("\n", index - 1) + 1;
            const lineEnd = text.indexOf("\n", end + closing.length);
            const aloneOnLine = !text.slice(lineStart, index).trim()
              && !text.slice(end + closing.length, lineEnd === -1 ? text.length : lineEnd).trim();
            result += opening === "\\[" && aloneOnLine
              ? `\n\n$$\n${formula}\n$$\n\n`
              : `$${formula}$`;
            index = end + closing.length;
            continue;
          }
        }
      }

      result += text[index];
      index += 1;
    }
    return result;
  }

  let result = "";
  let plainText = "";
  let fence: { marker: string; length: number } | null = null;
  for (const line of markdown.match(/[^\n]*(?:\n|$)/g) ?? [markdown]) {
    if (!line) continue;
    if (fence) {
      result += line;
      const closing = line.match(/^ {0,3}(`+|~+)[\t ]*\r?\n?$/)?.[1];
      if (closing?.[0] === fence.marker && closing.length >= fence.length) fence = null;
      continue;
    }
    const opening = line.match(/^ {0,3}(`{3,}|~{3,})/)?.[1];
    if (opening) {
      result += normalizeText(plainText) + line;
      plainText = "";
      fence = { marker: opening[0], length: opening.length };
    } else {
      plainText += line;
    }
  }
  return result + normalizeText(plainText);
}
