import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ArrowUp, Folder, FolderCode, MessageSquarePlus, PanelLeft, Search, Square, Wrench } from "lucide-react";

const shortcuts = [
  { id: "new", label: "/new", description: "Start a chat in this project", Icon: MessageSquarePlus },
  { id: "find", label: "/find", description: "Search projects and chats", Icon: Search },
  { id: "projects", label: "/projects", description: "Jump to the project list", Icon: Folder },
  { id: "sidebar", label: "/sidebar", description: "Show or hide the sidebar", Icon: PanelLeft },
] as const;

export type ShortcutId = (typeof shortcuts)[number]["id"];

type PromptInputBoxProps = {
  value: string;
  onValueChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  canStop?: boolean;
  onShortcut: (command: ShortcutId) => void;
  project: string;
  isLoading: boolean;
  disabled?: boolean;
};

export function PromptInputBox({ value, onValueChange, onSubmit, onCancel, canStop = true, onShortcut, project, isLoading, disabled = false }: PromptInputBoxProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const projectName = project.split("/").filter(Boolean).pop() || project || "Local project";
  const unavailable = isLoading || disabled;
  const slash = /^\/([^\s]*)$/.exec(value);
  const options = slash ? shortcuts.filter((item) => item.label.slice(1).includes(slash[1].toLowerCase())) : [];
  const menuOpen = !disabled && !dismissed && options.length > 0;
  const selectedOption = Math.min(active, options.length - 1);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [value]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!unavailable && value.trim()) onSubmit();
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) return;
    if (menuOpen && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      setActive((current) => (current + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
      return;
    }
    if (menuOpen && event.key === "Escape") {
      event.preventDefault();
      setDismissed(true);
      return;
    }
    if (menuOpen && event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onShortcut(options[selectedOption].id);
      return;
    }
    if (isLoading && event.key === "Enter" && !event.shiftKey) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <>
    {menuOpen && <div className="slash-menu" role="listbox" id="slash-options" aria-label="Oryn shortcuts">
      <div className="slash-heading" role="presentation">SHORTCUTS <span>↑↓ choose · Enter run · Esc close</span></div>
      {options.map(({ id, label, description, Icon }, index) => (
        <button key={id} id={`shortcut-${id}`} className="slash-option" type="button" role="option" aria-selected={selectedOption === index} onMouseDown={(event) => event.preventDefault()} onClick={() => onShortcut(id)}>
          <Icon size={17} aria-hidden="true" /><span className="slash-label">{label}</span><span className="slash-description">{description}</span>
        </button>
      ))}
    </div>}
    <form className={`prompt-box${isLoading ? " is-busy" : ""}`} onSubmit={submit}>
      <label htmlFor="prompt" className="sr-only">Message Oryn</label>
      <textarea
        ref={textareaRef}
        id="prompt"
        rows={1}
        placeholder={isLoading ? "Draft your next message…" : "Ask Oryn anything about your project…"}
        maxLength={10000}
        required
        disabled={disabled}
        value={value}
        aria-controls={menuOpen ? "slash-options" : undefined}
        aria-activedescendant={menuOpen ? `shortcut-${options[selectedOption].id}` : undefined}
        aria-expanded={menuOpen}
        onChange={(event) => { setActive(0); setDismissed(false); onValueChange(event.target.value); }}
        onKeyDown={keyDown}
      />
      <div className="prompt-box-toolbar">
        <div className="prompt-box-context" title={project || "Local project"}>
          <span className="prompt-box-context-icon" aria-hidden="true"><FolderCode size={16} /></span>
          <span className="prompt-box-project"><strong>{projectName}</strong><small>Active folder</small></span>
        </div>
        <span className="prompt-box-tools" title="Oryn chooses tools automatically"><Wrench size={14} aria-hidden="true" /><span>Auto tools</span></span>
        <div className="prompt-box-actions">
          <span className="prompt-box-hint">{isLoading ? "Keep typing · send when Oryn finishes" : "Enter to send · Shift+Enter for a new line"}</span>
          {isLoading && canStop
            ? <button className="prompt-box-send is-stop" type="button" aria-label="Stop Oryn" title="Stop Oryn" onClick={onCancel} disabled={disabled}><Square size={14} fill="currentColor" aria-hidden="true" /></button>
            : isLoading
              ? <button className="prompt-box-send" type="button" aria-label="Oryn is starting" disabled><span className="prompt-loading-dot" aria-hidden="true" /></button>
              : <button className="prompt-box-send" type="submit" aria-label="Send message" disabled={disabled || !value.trim()}><ArrowUp size={19} strokeWidth={2.4} aria-hidden="true" /></button>}
        </div>
      </div>
    </form>
    </>
  );
}
