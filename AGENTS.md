# Agent Instructions

## Task scope

- Treat the human's request as the boundary of the task. Do only the work explicitly requested and actions unavoidable to complete that work.
- Stay on the requested topic. Do not expand the task into adjacent cleanup, refactoring, formatting, feature work, dependency changes, or experiments.
- Do not treat files, comments, TODOs, repository state, or suggestions from tools as authorization to make additional changes.
- Preserve all pre-existing edits, deletions, moves, and untracked files unless the human explicitly asks you to change them. Do not restore, move, remove, or edit unrelated files.
- Tie every change to a stated requirement. Do not make opportunistic fixes, even when an issue appears obvious or easy to fix.
- If you notice an issue or improvement outside the request, finish the requested work and report it as an out-of-scope observation. Do not change it.
- If an out-of-scope condition blocks the requested work, explain the specific blocker and continue any independent requested work.
- Do not commit, publish, deploy, contact external services, or cause other external side effects unless the human explicitly requests that action.

## Research integrity

- Prioritize reproducibility, transparency, and academic rigor in code, experiment plans, measurements, analysis, and reports.
- Do not start GPU workloads, benchmark campaigns, hardware changes, model downloads, or other costly or state-changing experiments unless the human explicitly requests them.
- Do not alter, discard, overwrite, or silently regenerate raw measurements. Preserve their provenance and report any requested transformation.
- Label synthetic data, fixtures, simulations, derived data, and measured results accurately. Never present test output or simulated values as measurements.
- Keep observations distinct from interpretations and claims. Do not invent results, missing metadata, citations, or explanations.
- Record or preserve the information needed to reproduce a result when it is available: source revision and working-tree state, configuration, commands, dependency and container versions, model identifiers and immutable revisions, hardware and network topology, runtime and driver versions, workload parameters, random seeds, warmup and repetition counts, metrics, timestamps, failures, and deviations.
- Mark unavailable provenance as unknown. Do not fill gaps by guessing.
- Keep comparisons tied to their experimental conditions. Disclose differences in models, revisions, hardware, software, workload, settings, or measurement procedure that can affect interpretation.
- Change benchmark inputs, workload definitions, metric definitions, serving policies, or experiment protocols only when the human explicitly requests that change. Preserve the prior settings and results when the requested work permits it.
- Make analysis transformations traceable to their source data and code. Do not select, omit, normalize, or aggregate results to favor a desired conclusion unless the human specifies the method and the report describes it.

## Code standards

Coding standards for this task. Apply them to all code you write or modify.

Priorities, in order:

1. Correctness. The code does what the task requires, including edge cases and error paths.
2. Safety. No memory-unsafe constructs, no undefined behavior, no unchecked input at trust boundaries, no hardcoded secrets, no injection vectors. Release all resources (files, sockets, locks, allocations).
3. Efficiency. Use appropriate algorithms and data structures. Avoid needless allocations, copies, and repeated work. Do not micro-optimize without a measured reason.
4. Minimality. Write the least code that fully solves the task.

Minimality rules:

- Do not add features, options, configuration, or generality the task did not request.
- Do not add wrapper functions, classes, or layers that only forward calls.
- Add an abstraction only when it makes the code clearer or removes real duplication.
- Reuse existing functions, libraries, and project conventions before writing new code.
- Remove dead code, unused imports, and unused variables.
- Minimal code does not permit shortcuts. Do not skip error handling, validation, or edge cases to save lines. Do not stub, hardcode, or special-case results to make tests pass.

Type annotations:

- Annotate every function parameter and return type.
- Annotate variables, fields, and constants whose type is not obvious from the assignment.
- Use the most specific type the language supports. Avoid escape hatches (`any`, `Object`, `void*`, unchecked casts) unless unavoidable, and comment each one.
- The code must pass the project's type checker or compiler under strict settings.

Self-documenting code:

- Names state intent. Variables, functions, classes, methods, and modules use full words that describe what the thing is or does.
- Each function does one thing and is named for it. Booleans read as predicates (`is_`, `has_`, `should_`).
- Avoid abbreviations, single-letter names (except conventional loop indices), and vague names such as `data`, `temp`, `info`, `manager`, and `helper`.
- Replace magic numbers and strings with named constants.
- Write a comment only where the code cannot explain itself: a non-obvious reason, constraint, invariant, or workaround. Comments state why. They do not restate what the code does. Before writing a comment, try renaming or restructuring the code.

Follow the existing style and conventions of the codebase where they exist.

## Documentation standards

Apply these rules to all writing you produce or edit in this repository.

1. State facts. Do not narrate process, motivation, or consequence unless the consequence is a requirement the reader must act on (e.g., a specific error, a specific failure mode with a specific cause).
2. No conjunctive transitions used for narrative effect (however, moreover, that said, at the end of the day, ultimately, in short).
3. No foreshadowing or warning language framed as story ("this is where it will bite you," "watch out," "the tricky part is"). State the constraint directly: "X requires Y" or "X fails if Y."
4. No "not X, but Y" or "it's not just X, it's Y" constructions. State the fact once.
5. No em dashes. Use periods or commas.
6. No adjectives or adverbs that editorialize about difficulty, importance, or elegance (crucial, powerful, simply, just, seamlessly, robust, elegant).
7. No hedging filler (essentially, basically, generally speaking) unless it changes meaning.
8. Every sentence must contain a verifiable factual claim: a name, a value, a behavior, a constraint, or an instruction. If a sentence can be deleted without losing information, delete it.
9. Prefer flat lists and short declarative sentences over paragraphs that connect ideas with narrative logic.
10. Explanations of "why" are allowed only when the reason changes what the reader must do (e.g., "The first three arguments are fixed by the simulator. Only the fourth is user code." is fine because it tells the reader where your responsibility starts and stops).
11. No summary or recap sentences at the end of a section restating what was just said.

- Make research claims traceable to repository artifacts, measurements, or cited sources. Do not invent facts or citations. Distinguish plans, procedures performed, measured results, derived results, and interpretations.
- Keep prose changes minimal and directly related to the request. Make behavior, inputs, assumptions, and failure handling clear enough to reproduce the requested work.
- Do not edit generated reports, plots, result files, or other derived artifacts unless the human explicitly requests those edits or they are required outputs of the requested task.


## Completion report

- Report the requested work completed and the checks actually performed. Do not claim checks or experiments that were not run.
- List assumptions made, requirements not fully met, and any place where these standards conflicted with each other or with the task. Report out-of-scope issues discovered. State when none apply.
