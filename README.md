## Swaygentic

- A light snappy sandboxed harness I built for and with grok build.
- This is version number 300 something, it works decent so i decided to post it.
- works with debian, arch and derivatives.
- I initially built this for myself. A dream of using agentic ai for business tax purposes.
- Grok build now does all my state and federal taxes.
  
[Info]
- Invoking "swaygentic", starts grok build inside a tightly integrated bubblewrap sandbox with seccomp filtering.
- Brave origin nightly is the browser of choice.
- 27 mcp tools.
- Can be run with an ollama model, configured in agent.py.
- the ollama models are a bit shakey with complex browser work. I am building a dataset with grok to fine tune a vision model. I will include those in this repo once completed.
- This is browser/cli control only, no pixel clicking until i find a way to prevent focus stealing with a headed browser.
- To tune for maximum speed, disable all unneccessary plugins/skills in grok build. if you can afford the tokens, set model to grok 4.7 fast.
- When the agent is started with "swaygentic", STARTUP.md is automatically appended to system message with --rules. you can change it to --system-prompt-override in Swaygentic/bin/swaygentic to replace the default system message if needed.
  
[Install]
```bash
git clone https://github.com/backdoorsecurity/Swaygentic.git
cd Swaygentic
./install.sh
```
  
  
[Usage]  
  
cd Swaygentic
  
swaygentic
  
swaygentic --resume
