# Files as text

One viewer for everything that is text, with a reading for each way you might
want it. F3 opens the best one for the file; **Shift+F3 picks another**.

| reading | what it is for |
| --- | --- |
| Text | the file as it is, coloured by whatever language claims it |
| Markdown | headings, lists, tables and quotes, with the section you are in kept at the top |
| Formatted JSON | re-indented and coloured |
| JSON as table | a top-level array as rows and columns |
| Table | CSV and TSV, with the separator sniffed |
| Numbered lines | a log, one line per row, with its number beside it |
| Hex dump | the last resort, and what claims anything nothing else does |

## The languages

This plugin carries no code. What a language *looks like* is data — its
comments, its quotes, the words it reserves, and the patterns that say what a
whole line is — and the application reads a file once with that.

Sixty-one ship, one file each in `grammars/`:

- **Systems and applications** — C and C++ (and CUDA), C#, Go, Rust, Java,
  Kotlin, Swift, Objective-C, Scala, Dart, Zig, Nim, Pascal and Delphi,
  Fortran, Ada, Visual Basic, assembly.
- **Scripts** — Python, JavaScript and TypeScript, Ruby, PHP, Perl, Lua, R,
  Julia, Tcl, AWK, shell, PowerShell, Windows batch, Groovy and Gradle.
- **Functional** — Haskell, OCaml, F#, Elm, Elixir, Erlang, Clojure, Lisp and
  Scheme.
- **Shaders** — GLSL, HLSL with Unreal's `.usf` and `.ush` and Unity's, WGSL.
- **Hardware** — Verilog and SystemVerilog, VHDL.
- **Data and configuration** — SQL, GraphQL, Protocol Buffers, Terraform and
  HCL, Nix, Solidity, XML and HTML (Vue and Svelte among them), CSS, SCSS and
  Less, YAML, TOML, INI, CMake, Makefiles, Dockerfiles, LaTeX, Markdown, and
  the ignore lists git and its neighbours keep.

The next is a file in that folder, not a build. No two claim the same
extension: `.fs` is F#, not a fragment shader, and `.inc` and `.cls`, which
three languages would each have wanted, are left to plain text.

**A grammar names roles, never colours** — keyword, type, string, comment — and
what a role looks like comes from the appearance settings, so a file still
belongs to the palette you chose.

INI is the one worth pointing at: `[Section]` headings and the `+`/`-`/`.`/`!`
keys that Unreal's `config.ini` is made of are picked out on their own.

## Its one setting

**Weight of the text.** A shift of the interface's weight, on the same scale the
appearance settings use. A monospaced family reads thinner than the interface at
the same number, and this is the knob for that without moving the rest of the
application.

## What it will not do

It reads a file up to a limit — two megabytes as plain text, eight for JSON —
and says so when it stops. A search inside it searches what was read, and says
that too. Anything larger belongs to a tool that streams, which this is not.
