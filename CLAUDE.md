When using Azure DevOps MCP tools:

- Organization: rSTARTechnologies
- Project: AI Enablement Program

When running tests using playwright,

- Make sure the test case is followed correctly as per the steps
- If any step action fails, mark the test case as fail with reason and move on
- Create test data wherever required if not mentioned in the test case
- If you come across any action which is not mentioned in the test case but is required to complete the test case, skip that test case and mark it as NA with reason
- For each test case, create a folder with name TC_<TC_ID> and it must have a video recording evidence (recorded at realtime) of the execution for that test case along with the json evaluation result.
- So each test folder will have two files, one is video recording and another is json evaluation result.

Follow the exact below format for json evaluation result:

{
    "TC_ID": "<TC_ID>",
    "Result": "Pass/Fail/NA",
    "Reason": "<Reason for result"
}