import js from "@eslint/js";

export default [
  { ignores: ["dist/**", "src-tauri/target/**"] },
  js.configs.recommended,
  {
    files: ["src/**/*.js", "test/**/*.js"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { document: "readonly", window: "readonly", navigator: "readonly", Notification: "readonly", fetch: "readonly", setTimeout: "readonly", URL: "readonly" }
    }
  }
];
