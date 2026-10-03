# Resume Diagnosis

## ATS-killers
- [ ] No GitHub/portfolio link despite this being an AI Research Intern application — for a technical AI role, the absence of a GitHub URL (field is literally blank) means ATS keyword scans and recruiters have zero code artifacts to verify claims like 'Trained a Gemma 3.0 powered AI chatbot' or 'Built an AI pipeline.'
- [ ] Skills section has empty 'frameworks' array — no PyTorch, TensorFlow, JAX, Hugging Face, scikit-learn, etc. listed anywhere, which is an instant filter-out for any AI/ML research req that keyword-matches on framework names.
- [ ] Degree field is a run-on: 'Major: Systems Engineering and Design Track option: Robotics and Automated Systems; Minor: Computer Science' — ATS degree-parsers expect a clean 'field of study' string; this format risks mis-parsing into a garbled or truncated field, and colons/semicolons inside a single field confuse resume parsers that split on those characters.
- [ ] Overlapping/duplicate date ranges: EDRA Labs (June 2026–July 2026), Mondelez (Jan 2026–May 2026), and HCI Research (Jan 2026–Present) all sit in the same calendar window as each other and are dated in the future relative to a 'present' resume being evaluated in Sept 2026 in a way that reads as either forward-looking placeholders or unverified/fabricated timeline — ATS and recruiter alike will flag concurrent full research/R&D internships as a credibility red flag if not explicitly labeled part-time/remote.
- [ ] No standard 'Projects' section populated (empty array) despite Skills listing Python/C++/ML/Fusion 360 — tools without linked project evidence read as unsubstantiated keyword stuffing to both ATS relevancy scoring and human reviewers.
- [ ] Certifications list mixes unrelated credential types ('6 Sigma Green Belt', 'Phi Eta Sigma National Honor Society', 'IBDP') with no dates or issuing bodies — IBDP (International Baccalaureate Diploma Programme) is a high-school credential, which actively signals lack of seniority/relevance for a research internship and dilutes the certifications block ATS relevance score.

## Section-by-section diagnosis
### summary
> (No summary/objective section exists at all)

For an AI Research Intern role, ATS keyword-density scoring and recruiter 6-second scans both rely heavily on a top-of-resume summary packed with role-specific terms (e.g., 'machine learning', 'LLM fine-tuning', 'RLHF', 'PyTorch'). Its total absence means the resume opens cold with Education, wasting the highest-visibility real estate on the page.

### experience
> Leveraging the technology to improve user decision making speeds and reducing cognitive load on users by 15 points on the Nasa TLX scale.

Starts with a weak present-participle verb ('Leveraging') instead of a strong past-tense action verb, breaks parallel structure with surrounding bullets, and the metric ('15 points on the Nasa TLX scale') is presented without baseline or sample size, making it unverifiable and read as inflated. It also fails to name any specific AI/ML technique (model type, framework, data pipeline) that an AI Research Intern screener is scanning for.

### skills
> "tools": ["Fusion 360", "SEO", "Matlab", "Prompt engineering", "Python", "C++", "ML"]

This dumps CAD software (Fusion 360), marketing tooling (SEO), and vague buzzwords ('ML' is not a tool, 'Prompt engineering' is not a tool) into a single undifferentiated 'tools' bucket with no 'frameworks' or 'languages(programming)' separation. ATS systems parsing for ML framework keywords (PyTorch, TensorFlow, Hugging Face, scikit-learn, NumPy, Pandas) will find zero matches, causing the resume to underscore against nearly every AI research intern JD.

### education
> Major: Systems Engineering and Design Track option: Robotics and Automated Systems; Minor: Computer Science

Cramming major/track/minor into one unstructured run-on string inside the 'degree' field is a parsing hazard — ATS degree extraction commonly fails or truncates on the first punctuation mark, potentially registering only 'Major: Systems Engineering and Design Track option' as the degree, losing the Computer Science minor and Robotics specialization that are actually the most relevant keywords for an AI Research Intern req.

## Missing signals
- No mention of any ML framework or library (PyTorch, TensorFlow, JAX, Hugging Face Transformers, scikit-learn, NumPy/Pandas) anywhere in the resume — for an AI Research Intern this is the single largest gap.
- No evidence of published work, arXiv preprints, technical blog, Kaggle competitions, or open-source contributions — 'Publications' array is empty and there's no portfolio/GitHub link, so there is no way to independently verify any AI claim.
- No mention of specific research methodologies expected for AI research roles: dataset curation size, model architecture details (transformer, CNN, RNN), evaluation metrics (perplexity, BLEU, F1, accuracy), or compute environment (GPU/cloud: AWS, GCP, Azure, on-prem clusters).
- No quantification of the 'Gemma 3.0' RLHF project's scale — number of training conversations, model parameter count, compute hours, or comparison baseline — leaving the flagship AI bullet unverifiable.
- No coursework or projects listed that map to core ML/AI theory (e.g., linear algebra, probability & statistics, deep learning, NLP) — listed coursework is generic ('Discrete Structures', 'Optimization Models', 'Business side of Engineering') and does not signal AI research readiness.
- No advisor/PI name, lab name, or publication venue for the 'Human Computer Interaction Research' assistantship — academic research roles are normally credentialed by naming the supervising professor/lab, which recruiters and ATS both look for to validate legitimacy.
- No work authorization / visa status field filled in (authorization object is entirely empty) — for internship pipelines at many companies this is a mandatory screening field and its absence can cause auto-rejection or a recruiter follow-up delay.

## Top 5 fixes ranked by impact
1. **Add a dedicated 'Frameworks/Libraries' skill line with real ML tooling (PyTorch, TensorFlow, Hugging Face, scikit-learn, NumPy, Pandas) that are actually used in the Gemma RLHF and AI pipeline work — currently the frameworks array is empty, which is disqualifying for keyword-matched AI research reqs.**
   - Before: "tools": ["Fusion 360", "SEO", "Matlab", "Prompt engineering", "Python", "C++", "ML"]
   - After: "languages": ["Python", "C++", "MATLAB"], "frameworks": ["PyTorch", "Hugging Face Transformers", "scikit-learn", "Pandas/NumPy"], "tools": ["Fusion 360", "Prompt Engineering", "SEO Automation"]
2. **Rewrite the EDRA Labs RLHF bullet to quantify scale and name the actual technique/architecture instead of vague description, so ATS scoring on 'RLHF', 'fine-tuning', 'evaluation metric' keywords hits.**
   - Before: Trained a Gemma 3.0 powered AI chatbot using RLHF on conversations testing its ability to pick up social queues and accurately portray textual conversing ability.
   - After: Fine-tuned a Gemma 3.0 (7B) chatbot via RLHF on 5,000+ annotated conversations, improving social-cue recognition accuracy by X% against a baseline supervised model, evaluated using human preference scoring.
3. **Restructure the Education 'degree' field into clean, separately parseable Major/Minor/Track fields instead of one run-on string with embedded colons and semicolons.**
   - Before: Major: Systems Engineering and Design Track option: Robotics and Automated Systems; Minor: Computer Science
   - After: B.S. Systems Engineering and Design (Robotics & Automated Systems track); Minor in Computer Science
4. **Add a Projects section (currently empty) with at least 2 AI/ML-specific side projects that include GitHub links, since Skills claims Python/ML/Prompt Engineering with zero supporting evidence anywhere else outside work experience.**
   - Before: "projects": []
   - After: Add e.g. 'LLM Evaluation Harness (github.com/...) — built automated pipeline benchmarking 5 open-source LLMs on reasoning tasks using EleutherAI's lm-eval-harness'
5. **Resolve the overlapping/concurrent internship date ranges (EDRA Labs Jun–Jul 2026, Mondelez Jan–May 2026, HCI Research Jan 2026–Present) by clarifying part-time/remote status or correcting date errors, since simultaneous full internships without labels reads as a timeline credibility issue to both ATS date-validation and human reviewers.**
   - Before: Mondelez International — Jan 2026–May 2026; Human Computer Interaction Research — Jan 2026–Present (concurrent, unlabeled)
   - After: Mondelez International — Jan 2026–May 2026 (Full-time); Human Computer Interaction Research — Jan 2026–Present (Part-time, 10 hrs/week, remote)
