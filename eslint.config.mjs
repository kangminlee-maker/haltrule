// What the modules under ts/ may not name. The host - process, console, timers,
// fetch, every node: module - needs no rule: ts/tsconfig.json compiles them
// with no host types, so those names do not exist. What is left is the
// language's own short list of things that are not a function of their
// arguments: the clock, randomness, the locale, code built from text, the
// garbage collector, and the global object as a way around all of this.
import parser from "@typescript-eslint/parser";

export default [
  // The adapter may use Node; declaration files declare and do nothing.
  { ignores: ["ts/adapter/**", "**/*.d.ts"] },
  {
    files: ["ts/**/*.ts"],
    languageOptions: { parser },
    linterOptions: { reportUnusedDisableDirectives: "error", noInlineConfig: true },
    rules: {
      "no-restricted-globals": [
        "error",
        { name: "Date", message: "a clock: the caller measures, the policy is told" },
        { name: "Intl", message: "depends on the locale" },
        { name: "globalThis", message: "a way to every global" },
        { name: "Function", message: "code built from text" },
        { name: "WeakRef", message: "depends on the garbage collector" },
        { name: "FinalizationRegistry", message: "depends on the garbage collector" },
      ],
      "no-eval": "error",
      "no-new-func": "error",
      "no-restricted-properties": [
        "error",
        { object: "Math", property: "random", message: "randomness" },
        { property: "localeCompare", message: "depends on the locale" },
        { property: "toLocaleString", message: "depends on the locale" },
        { property: "toLocaleLowerCase", message: "depends on the locale" },
        { property: "toLocaleUpperCase", message: "depends on the locale" },
        { property: "toLocaleDateString", message: "depends on the locale" },
        { property: "toLocaleTimeString", message: "depends on the locale" },
      ],
      "no-restricted-syntax": [
        "error",
        { selector: "ImportExpression", message: "a dynamic import names its module at run time" },
      ],
    },
  },
];
