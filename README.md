> **Status:** early development. This README describes the full vision, and automatic job applications are not built yet. See [usage.md](usage.md) for what works today and how to set it up.

# Main Idea
This repo creates an agentic system that will find and apply to jobs that match a given description

## Basic Flow
The basic flow everytime the script is ran goes as follows
- find 20 job postings that match a specific input 
    - The user will give a brief description of what kind of jobs they are looking for 
    - some sort of log is kept to keep the status of the jobs that the user has already applied for, making sure to not repeat jobs or apply to anything twice
- for each job
    - use the user's master resume to create a tailored resume to that job posting, making sure to include all the key words that can be included
    - double check the created resume to evaluate for correctness (no hallucinations, everything written is actually accurate and extracted from the master resume)
    - put the information into the correct pdf format for that user
    - apply to that job for the user
- after all jobs have been applied to, generate a report for the user outlining
    - what jobs have been applied to
    - any potential points the user can learn or improve to make them stand out in the job search they are looking for (are their any trends in which the user is consistently lacking for exampel)

## The Tech
To implement this flow, an LLM workflow will be used. An initial LLM call is used to fetch the jobs, then an evlauator/optimizer loop is used to create the tailored resumes (note that this could potentially be done in parallel as well), afterwards the flow continues to put the resume in the correct format, apply to the job, and then generate the report. 

The agents in this system will need skills to create PDFs, be able to access the web, interact with the web (apply to a job for a user), and potentially others. 

Where possible, the agents will be based on simple LLM API calls rather than 3rd party agentic frameworks. 