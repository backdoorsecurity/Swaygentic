# session rules:

1. 
When starting a project:
 - We will cover as many details as possible before any code is generated.
 - The project will be broken down into phases.
 - Never use hard coded home path names in scripts, always $HOME or python os.home eq.
 - When building a project, always use a structured directory tree formation.
 - Never use a jumbled dir structure, always follow this strict structure:
 - Example:

main.py
   |docs/
   |    |
   |    |README.md
   |    |COMMENTS.md
   |    |OBJECTIVES.md
   |
   |toolbox/
           |
           |function1.py
           |function2.py
           |function3.py
           |function4.py
           |function5.py


2. 
when generating scripts:
 - Keep scripts clean and tidy, write functions and syntax as simple as possible.
 - Comments should contain ONLY what the function does.
 - NEVER add comments containing problems, solutions, or ideas. these go in separate files:
 - COMMENTS.md: if extra information needs recorded about a function, it should be added here and linked to the respective file/function.
 - OBJECTIVES.md: we will cover all known objectives during planning phase.
 - PROGRESS.md: during planning phase, we will identify details and break down phases. these phases will be recorded here in order: phase 1 -> bottom, last phase -> top.
 - README.md: this file should only be generated once all phases completed/project is ready for upload to github. this is NOT a notes file!

3.
Agent behavior:
- Keep replies consice and tidy, bulleted lists preferred over paragraphs.
- If any uncertainty, ask questions, map out every detail possible.
- Take it slow. better to implement one piece at a time than roughly toss an entire system together and spend hours of debugging.

4.
functions:
- functions which require follow up functions ex: "click, sleep, screenshot", should be chainloaded and timed rather than you having to manually input multiple functions.
- Keep the function calls simple, use these examples:
1. launch("binary","url")
2. click("window","x","y")
- All options/args which are routinely used should always be hardcoded into the function.

5.
cleanup:
When making changes, NEVER leave unused code/functions behind, check every file to ensure there no leftover references/link/functions for the unused code.
