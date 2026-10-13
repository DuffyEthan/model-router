Output demo.gif
Set Shell "zsh"
Set FontSize 15
Set Width 1200
Set Height 640
Set TypingSpeed 35ms
Set Theme "Catppuccin Mocha"
Hide
Type "for v in $(env | grep -o '^CLAUDE[A-Z_]*'); do unset $v; done; cd /private/tmp/router-demo && export PS1='$ ' && clear"
Enter
Type "claude --model opus --plugin-dir model-router/plugin"
Enter
Sleep 5s
Up
Enter
Sleep 5s
Show
Sleep 1s
Type "what does ls -la do"
Enter
Sleep 8s
Screenshot s1.png
Type "write a python function that parses ISO dates, with pytest tests"
Enter
Sleep 4s
Screenshot s2.png
Escape
Sleep 1s
Ctrl+U
Type "design a zero-downtime migration to shard our 5TB production postgres by tenant"
Enter
Sleep 4s
Screenshot s3.png
Escape
Sleep 1s
Ctrl+U
Type "/router"
Enter
Sleep 4s
Screenshot s4.png
Sleep 2s
