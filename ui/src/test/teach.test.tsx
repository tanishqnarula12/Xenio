import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Activity, Lesson, LessonQuestion } from "../api/types";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { Inline, RichText } from "../components/RichText";
import { AppController } from "../state/controller";
import { AppProvider } from "../state/context";
import { Store } from "../state/store";
import { ActivityView, Calculator, Flashcards, Frames, GraphExplorer, MatchGame, OrderGame, StepThrough } from "../teach/activities";
import { CodeBlock, CodeRunner, highlight } from "../teach/code";
import { compile, format } from "../teach/expression";
import { DiagramView, LessonPlayer, QuestionCard } from "../teach/LessonView";
import { cleanSvg, MermaidView } from "../teach/Mermaid";
import { FakeApi, renderApp } from "./fakes";

function within_app(api: FakeApi, node: React.ReactNode) {
  const store = new Store();
  const app = new AppController(api, store);
  return render(<AppProvider store={store} app={app}>{node}</AppProvider>);
}

const question = (over: Partial<LessonQuestion> = {}): LessonQuestion => ({ id: 1, kind: "mcq", question: "Oxygen comes from?", options: ["CO2", "Water"], hints: [], hints_left: 1, state: "open", ...over } as LessonQuestion);

const lesson = (over: Partial<Lesson["pack"]> = {}): Lesson => ({
  id: 7, topic: "Photosynthesis", topic_id: 1, questions: [question()],
  pack: { title: "How plants eat", overview: "Plants build food from light.", why_it_matters: "All food starts here.", analogy: "A solar kitchen.", sections: [{ heading: "Light", body: "Light splits **water**." }, { heading: "Sugar", body: "Then sugar is built." }],
    worked_example: { problem: "6 CO2", steps: ["first", "second"], result: "one glucose" }, diagrams: [], activities: [], flashcards: [{ q: "Where?", a: "Chloroplast" }], common_mistakes: [{ mistake: "Oxygen from CO2", why: "It feels right", fix: "It comes from water" }],
    next_steps: ["The *Calvin* cycle"], uncertain: ["Rates vary"], ...over },
} as unknown as Lesson);

describe("formulas are read, never run", () => {
  it.each([["1 + 2 * 3", 7], ["(1 + 2) * 3", 9], ["2 ^ 3 ^ 2", 512], ["-x ^ 2", -4], ["sqrt(16) + abs(-2)", 6], ["max(1, x, 3)", 3], ["pi", Math.PI]])("%s", (source, want) => {
    expect(compile(source)({ x: 2 })).toBeCloseTo(want);
  });

  it.each(["alert(1)", "constructor", "x.y", "x; y", "window", "this", "1 +", "()=>1", "`x`", "[1]", "__proto__"])("%s is refused", (source) => {
    expect(() => compile(source)({ x: 1 })).toThrow();
  });

  it("formats without noise", () => {
    expect(format(3)).toBe("3");
    expect(format(1 / 3, 2)).toBe("0.33");
  });
});

describe("a diagram's picture is cleaned before it is shown", () => {
  it("takes away what could run, and keeps the labels", () => {
    const dirty = `<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><a href="javascript:alert(1)"><text>gone</text></a><g onclick="alert(1)"><text>Light</text></g><image href="http://evil/x.png"/><animate attributeName="x"/><foreignObject><div onmouseover="alert(1)" class="label"><iframe src="http://evil"></iframe><span style="color:red">Sugar</span></div></foreignObject></svg>`;
    const clean = cleanSvg(dirty);
    expect(clean).not.toMatch(/script|onclick|onmouseover|iframe|javascript|<image|<animate|evil/i);
    expect(clean).toContain("Light");
    expect(clean).toContain("Sugar");
  });

  it("nothing is what comes of something that is not a picture", () => {
    expect(cleanSvg("<svg><not closed")).toBe("");
  });

  it("if it cannot be drawn, it is given in words", async () => {
    vi.doMock("mermaid", () => ({ default: { initialize() {}, render: async () => { throw new Error("no"); } } }));
    render(<MermaidView source="flowchart TD; a-->b" alt="Light leads to sugar" />);
    expect(await screen.findByText(/could not be drawn/, undefined, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByText("Light leads to sugar")).toBeInTheDocument();
    vi.doUnmock("mermaid");
  }, 10000);

  it("a comparison and a timeline are real tables and lists", () => {
    render(<DiagramView diagram={{ kind: "comparison", title: "T", columns: ["", "A", "B"], rows: [["Speed", "fast", "slow"]] } as never} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Speed" })).toBeInTheDocument();
  });
});

describe("text in a lesson", () => {
  it("shows emphasis instead of asterisks, and leaves arithmetic alone", () => {
    const { container } = render(<p><Inline text="how *strongly* they respond, **bold**, `code`, 2*3*4 and 2 * 3 * 4" /></p>);
    expect(container.querySelector("em")).toHaveTextContent("strongly");
    expect(container.querySelector("strong")).toHaveTextContent("bold");
    expect(container.querySelector("code")).toHaveTextContent("code");
    expect(container.textContent).toContain("2*3*4 and 2 * 3 * 4");
  });

  it("markup in a model's words stays words", () => {
    const { container } = render(<p><Inline text={'<img src=x onerror="alert(1)"> *<b>hi</b>*'} /></p>);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("a code fence keeps its lines, and text after it is text again", () => {
    const { container } = render(<RichText text={"Before\n\n```python\nfor i in x:\n    print(i)\n```\n\nAfter **this**"} />);
    expect(container.querySelector("code")?.textContent).toContain("for i in x:\n    print(i)");
    expect(container.querySelector("strong")).toHaveTextContent("this");
  });

  it("colours code without running it", () => {
    render(<CodeBlock code={'print("hi")'} language="python" />);
    expect(screen.getByRole("button", { name: /Copy/ })).toBeInTheDocument();
    expect(highlight('x = "<script>"', "python")).toBeTruthy();
  });
});

describe("a well-formatted answer", () => {
  it("shows headings as headings, not as words with hashes", () => {
    render(<RichText text={"## How it works\n\nSome words.\n\n### A detail\nMore words."} />);
    expect(screen.getByRole("heading", { level: 3, name: "How it works" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 4, name: "A detail" })).toBeInTheDocument();
    expect(screen.queryByText(/#/)).toBeNull();
  });

  it("shows a table as a table, its cells formatted", () => {
    render(<RichText text={"| | TCP | UDP |\n|---|---|---|\n| **Order** | kept | not kept |\n| Speed | slower | faster |\n\nAfter."} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "TCP" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Order" }).querySelector("strong")).toHaveTextContent("Order");
    expect(screen.getByRole("cell", { name: "not kept" })).toBeInTheDocument();
    expect(screen.getByText("After.")).toBeInTheDocument();
    expect(screen.queryByText(/\|/)).toBeNull();
  });

  it("leaves a line with a pipe in it as words when it is no table", () => {
    render(<RichText text={"use a | b for that"} />);
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText("use a | b for that")).toBeInTheDocument();
  });

  it("keeps the numbers of steps split by blank lines", () => {
    const { container } = render(<RichText text={"1. First\n\n2. Second\n\n3. Third"} />);
    expect(Array.from(container.querySelectorAll("ol")).map((o) => o.getAttribute("start"))).toEqual(["1", "2", "3"]);
  });

  it("shows quotes and rules, and markup in them stays words", () => {
    const { container } = render(<RichText text={"> a *wise* <b>saying</b>\n\n---\n\nnext"} />);
    expect(container.querySelector("blockquote em")).toHaveTextContent("wise");
    expect(container.querySelector("blockquote b")).toBeNull();
    expect(container.querySelector("hr")).not.toBeNull();
  });

  it("shows a formula written in LaTeX as a formula", () => {
    const { container } = render(<p><Inline text={"Water is $H_2O$, so $6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6$ and $x^2$"} /></p>);
    expect(container.textContent).toBe("Water is H2O, so 6CO2 + 6H2O → C6H12O6 and x2");
    expect(container.querySelectorAll("sub").length).toBe(6);
    expect(container.querySelector("sup")).toHaveTextContent("2");
    expect(container.textContent).not.toContain("$");
  });

  it.each(["It costs $5 and $10 today", "from $5-$10", "pay $20.", "a $ sign", "US$ and $"])("leaves money alone: %s", (words) => {
    const { container } = render(<p><Inline text={words} /></p>);
    expect(container.textContent).toBe(words);
    expect(container.querySelector(".rich__formula")).toBeNull();
  });

  it("markup inside a formula stays words", () => {
    const { container } = render(<p><Inline text={"$<img src=x onerror=alert(1)>$"} /></p>);
    expect(container.querySelector("img")).toBeNull();
  });

  it("a diagram that cannot be drawn shows what it says, never a blank", async () => {
    vi.doMock("mermaid", () => ({ default: { initialize() {}, render: async () => { throw new Error("no"); } } }));
    render(<MermaidView source={"flowchart TD; a-->b"} alt="x" showSource />);
    expect(await screen.findByText(/could not be drawn/, undefined, { timeout: 8000 })).toBeInTheDocument();
    expect(screen.getByText(/flowchart TD; a-->b/)).toBeInTheDocument();
    vi.doUnmock("mermaid");
  }, 10000);
});

describe("one broken part does not take the page with it", () => {
  function Bomb(): never { throw new Error("boom"); }

  it("says which part, keeps the rest, and can try again", async () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    function Page() {
      const [safe, setSafe] = useState(false);
      return <><p>The rest of the lesson</p><button onClick={() => setSafe(true)}>fix</button><ErrorBoundary what="This activity">{safe ? <p>Works now</p> : <Bomb />}</ErrorBoundary></>;
    }
    render(<Page />);
    expect(screen.getByRole("alert")).toHaveTextContent("This activity could not be shown.");
    expect(screen.getByText("The rest of the lesson")).toBeInTheDocument();
    await userEvent.click(screen.getByText("fix"));
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Works now")).toBeInTheDocument();
    quiet.mockRestore();
  });

  it("a whole lesson stays up when one activity is broken", async () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const api = new FakeApi();
    api.lesson.mockResolvedValue(lesson({ activities: [{ type: "calculator", title: "Broken", inputs: null, outputs: null } as never] }));
    within_app(api, <LessonPlayer id={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Show it all" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be shown");
    expect(screen.getByRole("heading", { name: "How plants eat" })).toBeInTheDocument();
    quiet.mockRestore();
  });
});

describe("things to do", () => {
  it("a graph moves with its slider, and says what it shows", async () => {
    const a = { type: "graph", title: "Line", prompt: "Move a", functions: [{ expr: "a*x", label: "y" }], params: [{ name: "a", label: "a", min: 0, max: 4, step: 1, value: 1 }], x: { min: 0, max: 10 }, y: {} } as unknown as Extract<Activity, { type: "graph" }>;
    within_app(new FakeApi(), <GraphExplorer a={a} />);
    const graph = screen.getByRole("img");
    expect(graph.getAttribute("aria-label")).toContain("at x = 10 it is 10");
    fireEvent.change(screen.getByRole("slider", { name: "a" }), { target: { value: "2" } });
    expect(graph.getAttribute("aria-label")).toContain("at x = 10 it is 20");
    await userEvent.click(screen.getByRole("button", { name: /Reset/ }));
    expect(graph.getAttribute("aria-label")).toContain("at x = 10 it is 10");
  });

  it("a calculator works out the real formula, money before the number", async () => {
    const a = { type: "calculator", title: "Price", inputs: [{ name: "a", label: "Cost", value: 2 }], outputs: [{ label: "Total", expr: "a * 3", unit: "$", digits: 2 }, { label: "Weight", expr: "a", unit: "kg" }] } as unknown as Extract<Activity, { type: "calculator" }>;
    render(<Calculator a={a} />);
    expect(screen.getByText("$6")).toBeInTheDocument();
    expect(screen.getByText("2 kg")).toBeInTheDocument();
    const input = screen.getByRole("spinbutton", { name: /Cost/ });
    await userEvent.clear(input);
    await userEvent.type(input, "5");
    expect(screen.getByText("$15")).toBeInTheDocument();
  });

  it("steps ask you to think before they show", async () => {
    const a = { type: "steps", title: "S", steps: [{ title: "One", predict: "What next?", body: "The answer." }, { title: "Two", body: "More." }] } as unknown as Extract<Activity, { type: "steps" }>;
    render(<StepThrough a={a} />);
    expect(screen.getByText("What next?")).toBeInTheDocument();
    expect(screen.queryByText("The answer.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show me" }));
    expect(screen.getByText("The answer.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(screen.getByText("More.")).toBeInTheDocument();
  });

  it("frames step through, and their words are announced", async () => {
    const a = { type: "frames", title: "F", frames: [{ cells: ["1", "2"], note: "start", highlight: [0] }, { cells: ["1", "2"], note: "end", highlight: [1], pointers: { mid: 1 } }] } as unknown as Extract<Activity, { type: "frames" }>;
    within_app(new FakeApi(), <Frames a={a} />);
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText("start").closest("[aria-live]")).toHaveAttribute("aria-live", "polite");
    await userEvent.click(screen.getByRole("button", { name: "Next frame" }));
    expect(screen.getByText("end")).toBeInTheDocument();
    expect(screen.getByText("mid")).toBeInTheDocument();
  });

  it("matching counts the wrong tries", async () => {
    const a = { type: "match", title: "M", pairs: [["Sun", "Star"], ["Earth", "Planet"]] } as unknown as Extract<Activity, { type: "match" }>;
    render(<MatchGame a={a} />);
    await userEvent.click(screen.getByRole("button", { name: "Sun" }));
    await userEvent.click(screen.getByRole("button", { name: "Planet" }));
    await userEvent.click(screen.getByRole("button", { name: "Star" }));
    await userEvent.click(screen.getByRole("button", { name: "Earth" }));
    await userEvent.click(screen.getByRole("button", { name: "Planet" }));
    expect(screen.getByRole("status")).toHaveTextContent("All matched, with 1 wrong try");
  });

  it("ordering is checked against the true order, by keyboard buttons", async () => {
    const a = { type: "order", title: "O", items: ["First", "Second", "Third"] } as unknown as Extract<Activity, { type: "order" }>;
    const { container } = render(<OrderGame a={a} />);
    for (let tries = 0; tries < 12; tries += 1) {
      const now = Array.from(container.querySelectorAll(".order__text")).map((n) => n.textContent);
      const at = now.findIndex((t, i) => t !== a.items[i]);
      if (at < 0) break;
      await userEvent.click(screen.getByRole("button", { name: `Move "${a.items[at]}" up` }));
    }
    await userEvent.click(screen.getByRole("button", { name: /Check the order/ }));
    expect(container.querySelectorAll(".is-right").length).toBe(3);
  });

  it("flashcards flip and move on", async () => {
    render(<Flashcards cards={[{ q: "Q1", a: "A1" }, { q: "Q2", a: "A2" }]} />);
    expect(screen.getByText("Q1")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Q1"));
    expect(screen.getByText("A1")).toBeInTheDocument();
  });

  it("something it does not know how to show is left out, not faked", () => {
    const { container } = render(<ActivityView a={{ type: "hologram" } as never} />);
    expect(container.textContent).toBe("");
  });
});

describe("the code terminal", () => {
  const running = (over = {}) => ({ ok: true, result: { ok: true, stdout: "4\n", stderr: "", exit: 0, ms: 18, timed_out: false, phase: "run", ...over } });

  it("runs only on Run, with your code and your input, and shows what really came out", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue(running() as never);
    within_app(api, <CodeRunner language="python" code="print(2+2)" stdin="abc" lesson={1} index={0} />);
    expect(api.changeLearning).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    expect(api.changeLearning).toHaveBeenCalledWith(expect.objectContaining({ action: "run_code", language: "python", code: "print(2+2)", stdin: "abc" }));
    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.getByText(/Exit 0 · 18 ms/)).toBeInTheDocument();
    expect(screen.getByText(/no network and no access to your files/)).toBeInTheDocument();
  });

  it.each([[{ timed_out: true, exit: -9, stdout: "" }, /ran for too long/], [{ ok: false, phase: "compile", stdout: "", stderr: "error: expected ;" }, /did not compile/], [{ stdout: "" }, /printed nothing/]])("reports honestly: %j", async (over, words) => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue(running(over) as never);
    within_app(api, <CodeRunner language="cpp" code="x" lesson={1} index={0} />);
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    expect(await screen.findByText(words)).toBeInTheDocument();
  });

  it("an exercise is marked by what really ran, and the solution waits until you tried", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue({ ok: true, passed: 1, total: 2, all_passed: false, results: [{ name: "small", stdin: "1", expected: "2", got: "2", passed: true }, { name: "big", stdin: "9", expected: "10", got: "None", passed: false }] } as never);
    within_app(api, <CodeRunner language="python" code="pass" checks={[{ stdin: "1", expected: "2" }]} solution="the answer" lesson={1} index={2} />);
    expect(screen.queryByRole("button", { name: /Show the solution/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Check my code/ }));
    expect(api.changeLearning).toHaveBeenCalledWith(expect.objectContaining({ action: "check_code", lesson: 1, index: 2 }));
    const report = await screen.findByText("1 of 2 checks passed.");
    expect(within(report.closest(".checks") as HTMLElement).getByText("None")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Show the solution/ }));
    expect(screen.getByText(/the answer/)).toBeInTheDocument();
  });

  it("a strange answer from the server does not empty the page", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue({ ok: true } as never);
    within_app(api, <CodeRunner language="python" code="pass" checks={[{ stdin: "1", expected: "2" }]} lesson={1} index={2} />);
    await userEvent.click(screen.getByRole("button", { name: /Check my code/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Check my code/ })).toBeEnabled());
    expect(screen.getByRole("button", { name: /^Run/ })).toBeInTheDocument();
  });

  it("a failed request is shown", async () => {
    const api = new FakeApi();
    api.changeLearning.mockRejectedValue(new Error("the sandbox is not available"));
    within_app(api, <CodeRunner language="python" code="pass" lesson={1} index={0} />);
    await userEvent.click(screen.getByRole("button", { name: /^Run/ }));
    expect(await screen.findByText(/the sandbox is not available/)).toBeInTheDocument();
  });
});

describe("a question", () => {
  it("records your answer and shows what the server decided, never its own guess", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue({ ok: true, credit: 0, feedback: "Not from CO2.", correct_answer: "Water" } as never);
    within_app(api, <QuestionCard question={question()} />);
    await userEvent.click(screen.getByRole("button", { name: /CO2/ }));
    expect(api.changeLearning).toHaveBeenCalledWith(expect.objectContaining({ action: "answer", id: 1, answer: "CO2" }));
    expect(await screen.findByText("Not quite.")).toBeInTheDocument();
    expect(screen.getByText("Not from CO2.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Water/ })).toHaveClass("is-right");
    expect(screen.getByRole("button", { name: /CO2/ })).toHaveClass("is-wrong");
  });

  it("hints come before the answer", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue({ ok: true, hint: "Think of water.", hints_left: 0 } as never);
    within_app(api, <QuestionCard question={question()} />);
    await userEvent.click(screen.getByRole("button", { name: "A hint" }));
    expect(await screen.findByText(/Think of water/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /hint/i })).toBeNull();
    expect(screen.getByRole("button", { name: "Show the answer" })).toBeInTheDocument();
  });

  it("seeing the answer first is said to count for nothing", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue({ ok: true, answer: "Water", explanation: "It splits." } as never);
    within_app(api, <QuestionCard question={question()} />);
    await userEvent.click(screen.getByRole("button", { name: "Show the answer" }));
    expect(await screen.findByText(/Seen before answering/)).toBeInTheDocument();
  });

  it("an open question takes your own words", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue({ ok: true, credit: 1, feedback: "Yes." } as never);
    within_app(api, <QuestionCard question={question({ kind: "open", options: [] })} />);
    expect(screen.getByRole("button", { name: "Check my answer" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Your answer"), "From water");
    await userEvent.click(screen.getByRole("button", { name: "Check my answer" }));
    expect(await screen.findByText("Right.")).toBeInTheDocument();
  });
});

describe("a lesson", () => {
  it("goes one step at a time, or all at once, and says what could not be written", async () => {
    const api = new FakeApi();
    api.lesson.mockResolvedValue(lesson({ missing: ["activities"] }));
    within_app(api, <LessonPlayer id={7} />);
    expect(await screen.findByRole("heading", { name: "How plants eat" })).toBeInTheDocument();
    expect(screen.getByText(/could not be written this time: activities/)).toBeInTheDocument();
    expect(screen.queryByText("Then sugar is built.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await userEvent.click(screen.getByRole("button", { name: "Show it all" }));
    expect(await screen.findByText("Then sugar is built.")).toBeInTheDocument();
    expect(screen.getByText("Where to go next")).toBeInTheDocument();
    expect(screen.getByText("Calvin").tagName).toBe("EM");
    expect(screen.getByText("What to double-check")).toBeInTheDocument();
    expect(screen.queryByText(/\*/)).toBeNull();
  });

  it("a lesson that cannot be opened says so", async () => {
    const api = new FakeApi();
    api.lesson.mockRejectedValue(new Error("that lesson is not there any more"));
    within_app(api, <LessonPlayer id={7} />);
    expect(await screen.findByText(/not there any more/)).toBeInTheDocument();
  });
});

describe("a lesson's questions", () => {
  it("are together at the end, never popping up between its sections", async () => {
    const api = new FakeApi();
    api.lesson.mockResolvedValue(lesson());
    within_app(api, <LessonPlayer id={7} />);
    await userEvent.click(await screen.findByRole("button", { name: "Show it all" }));
    const question = await screen.findByText("Oxygen comes from?");
    const heading = screen.getByRole("heading", { name: "Show what you know" });
    expect(heading.compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Sugar" }).compareDocumentPosition(question) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("the Learn page", () => {
  it("is in the navigation, and starts empty with no made-up progress", async () => {
    const api = new FakeApi();
    await renderApp(api, "learn");
    expect(await screen.findByRole("heading", { name: "Learn" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Learn" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText(/Nothing yet/)).toBeInTheDocument();
  });

  it("shows subjects with what was really shown, and what is due", async () => {
    const api = new FakeApi();
    api.learning.mockResolvedValue({ topics: [{ id: 3, name: "Gravity", concepts: 4, demonstrated: 1, due: 2 }], modes: { teach: "Teach me" }, default_mode: "teach", writer: true, style: "", due: [{ concept: "Orbits", topic: "Gravity", topic_id: 3 }] } as never);
    await renderApp(api, "learn");
    expect(await screen.findByText("Gravity")).toBeInTheDocument();
    expect(screen.getByText(/1 of 4 shown · 2 due/)).toBeInTheDocument();
    expect(screen.getByText("Orbits")).toBeInTheDocument();
  });
});

describe("files in the chat", () => {
  const file = (name: string, kind = "text/plain") => new File(["hello"], name, { type: kind });
  const composer = () => document.getElementById("composer-input") as HTMLTextAreaElement;

  it("a file can be added, shows as a chip, and goes with the message", async () => {
    const api = new FakeApi();
    await renderApp(api, "home");
    const box = await screen.findByLabelText("Add files", { selector: "input" });
    await userEvent.upload(box, file("notes.txt"));
    expect(api.upload).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    await userEvent.type(composer(), "study this");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(api.chat).toHaveBeenCalled());
    expect(api.chat.mock.calls[0]![0]).toBe("study this");
    expect(api.chat.mock.calls[0]).toContainEqual(["f1"]);
  });

  it("a file alone can be sent", async () => {
    const api = new FakeApi();
    await renderApp(api, "home");
    await userEvent.upload(await screen.findByLabelText("Add files", { selector: "input" }), file("notes.txt"));
    await screen.findByText("notes.txt");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.chat).toHaveBeenCalled());
  });

  it("a failed upload says why and the message is not lost", async () => {
    const api = new FakeApi();
    api.upload.mockRejectedValue(new Error("That file is too large: 80 MB is the limit."));
    await renderApp(api, "home");
    await screen.findByLabelText("Add files", { selector: "input" });
    await userEvent.type(composer(), "keep me");
    await userEvent.upload(screen.getByLabelText("Add files", { selector: "input" }), file("big.pdf", "application/pdf"));
    expect(await screen.findByText(/too large/)).toBeInTheDocument();
    expect(composer()).toHaveValue("keep me");
  });

  it("no more than five in one message", async () => {
    const api = new FakeApi();
    await renderApp(api, "home");
    await userEvent.upload(await screen.findByLabelText("Add files", { selector: "input" }), Array.from({ length: 7 }, (_, i) => file(`f${i}.txt`)));
    expect(await screen.findByText(/At most 5 files/)).toBeInTheDocument();
    expect(api.upload).toHaveBeenCalledTimes(5);
  });
});
