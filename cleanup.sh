uv cache clean
rm -rf ./build
rm -rf mcp.json
npx playwright uninstall
uvx --from git+https://github.com/nandatheguntupalli/web-eval-agent.git webEvalAgent

#   op-ONQvLZNIwG59yubZPeqpxS5jns8C9Xdd2qtZmIc1ReM