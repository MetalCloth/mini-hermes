import { useState } from "react";
import { PromptInputBox } from "./ai-prompt-box";

export function AiPromptBoxDemo() {
  const [value, setValue] = useState("");
  const [sent, setSent] = useState("");

  return (
    <div className="flex min-h-[360px] w-full flex-col items-center justify-center gap-4 bg-[#f8faff] p-4">
      <div className="w-full max-w-[700px]">
        <PromptInputBox
          value={value}
          onValueChange={setValue}
          onSubmit={() => { setSent(value.trim()); setValue(""); }}
          onCancel={() => {}}
          onShortcut={(command) => { setSent(`Ran /${command}`); setValue(""); }}
          project="/your/project"
          isLoading={false}
        />
      </div>
      {sent && <p className="text-sm text-slate-600">Sent: {sent}</p>}
    </div>
  );
}
