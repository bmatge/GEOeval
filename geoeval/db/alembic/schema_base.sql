-- Instantané du schéma GEOeval au lot 1.4 (ADR-088), produit par
-- `pg_dump --schema-only --no-owner --no-privileges` sur une base vierge construite
-- par l'ancien chemin init_db (create_all) → migrations.sql, puis nettoyé.
--
-- Joué UNIQUEMENT par la révision Alembic 0001 sur une base vierge. Sur une base
-- existante, 0001 rejoue migrations.sql (gelé) pour converger vers ce même état.
-- NE PAS MODIFIER : toute évolution du schéma est une nouvelle révision Alembic.
CREATE TABLE public.api_tokens (
    id integer NOT NULL,
    organization_id integer NOT NULL,
    name text NOT NULL,
    role text NOT NULL,
    prefix text NOT NULL,
    token_hash text NOT NULL,
    created_by integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);
CREATE SEQUENCE public.api_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.api_tokens_id_seq OWNED BY public.api_tokens.id;
CREATE TABLE public.audit_log (
    id integer NOT NULL,
    user_id integer,
    org_id integer,
    action text NOT NULL,
    entity_type text NOT NULL,
    entity_id integer,
    at timestamp with time zone DEFAULT now() NOT NULL,
    meta_json jsonb
);
CREATE SEQUENCE public.audit_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;
CREATE TABLE public.auth_tokens (
    id integer NOT NULL,
    user_id integer NOT NULL,
    purpose text DEFAULT 'set_password'::text NOT NULL,
    token text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    used_at timestamp with time zone
);
CREATE SEQUENCE public.auth_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.auth_tokens_id_seq OWNED BY public.auth_tokens.id;
CREATE TABLE public.budgets (
    organization_id integer NOT NULL,
    monthly_cap_eur numeric(12,2) NOT NULL,
    daily_cap_eur numeric(12,2),
    currency text DEFAULT 'EUR'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by integer
);
CREATE TABLE public.evaluation_prompts (
    prompt_id integer NOT NULL,
    prompt_type_id integer NOT NULL,
    prompt_name text NOT NULL,
    prompt_text text NOT NULL
);
CREATE SEQUENCE public.evaluation_prompts_prompt_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.evaluation_prompts_prompt_id_seq OWNED BY public.evaluation_prompts.prompt_id;
CREATE TABLE public.gold_annotations (
    id integer NOT NULL,
    test_id integer NOT NULL,
    run_id integer NOT NULL,
    ground_truth_version integer,
    annotator_email text NOT NULL,
    response_label text NOT NULL,
    response_score numeric(4,2) NOT NULL,
    citation_label text NOT NULL,
    citation_score numeric(4,2) NOT NULL,
    notes text,
    annotated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.gold_annotations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.gold_annotations_id_seq OWNED BY public.gold_annotations.id;
CREATE TABLE public.invitations (
    id integer NOT NULL,
    org_id integer NOT NULL,
    email text NOT NULL,
    role text NOT NULL,
    invited_by integer NOT NULL,
    token text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    accepted_at timestamp with time zone
);
CREATE SEQUENCE public.invitations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.invitations_id_seq OWNED BY public.invitations.id;
CREATE TABLE public.job_logs (
    id integer NOT NULL,
    job_id text NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    level text NOT NULL,
    message text NOT NULL
);
CREATE SEQUENCE public.job_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.job_logs_id_seq OWNED BY public.job_logs.id;
CREATE TABLE public.jobs (
    id text NOT NULL,
    organization_id integer NOT NULL,
    status text NOT NULL,
    priority integer NOT NULL,
    params jsonb NOT NULL,
    phase text NOT NULL,
    current integer NOT NULL,
    total integer NOT NULL,
    error text,
    run_ids jsonb NOT NULL,
    attempt integer NOT NULL,
    worker_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    claimed_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    finished_at timestamp with time zone
);
CREATE TABLE public.memberships (
    user_id integer NOT NULL,
    org_id integer NOT NULL,
    role text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.model_pricing (
    id integer NOT NULL,
    model_id integer NOT NULL,
    input_price_per_1m_tokens numeric(12,6) NOT NULL,
    output_price_per_1m_tokens numeric(12,6) NOT NULL,
    currency text DEFAULT 'EUR'::text NOT NULL,
    effective_from timestamp with time zone DEFAULT now() NOT NULL,
    effective_to timestamp with time zone
);
CREATE SEQUENCE public.model_pricing_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.model_pricing_id_seq OWNED BY public.model_pricing.id;
CREATE TABLE public.models (
    model_id integer NOT NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    base_url text,
    api_key text,
    extra_headers jsonb,
    search_config jsonb,
    is_active boolean DEFAULT true NOT NULL,
    is_judge boolean DEFAULT true NOT NULL,
    is_sovereign boolean DEFAULT false NOT NULL
);
CREATE SEQUENCE public.models_model_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.models_model_id_seq OWNED BY public.models.model_id;
CREATE TABLE public.org_credentials (
    id integer NOT NULL,
    organization_id integer NOT NULL,
    model_id integer NOT NULL,
    base_url text,
    api_key_encrypted text,
    extra_headers jsonb,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.org_credentials_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.org_credentials_id_seq OWNED BY public.org_credentials.id;
CREATE TABLE public.org_models (
    id integer NOT NULL,
    organization_id integer NOT NULL,
    model_id integer NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.org_models_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.org_models_id_seq OWNED BY public.org_models.id;
CREATE TABLE public.organizations (
    id integer NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by integer
);
CREATE SEQUENCE public.organizations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.organizations_id_seq OWNED BY public.organizations.id;
CREATE TABLE public.perimeters (
    id integer NOT NULL,
    organization_id integer NOT NULL,
    name text NOT NULL,
    slug text NOT NULL,
    kind text,
    home_url text,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by integer
);
CREATE SEQUENCE public.perimeters_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.perimeters_id_seq OWNED BY public.perimeters.id;
CREATE TABLE public.prompt_types (
    prompt_type_id integer NOT NULL,
    prompt_type_label text NOT NULL
);
CREATE SEQUENCE public.prompt_types_prompt_type_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.prompt_types_prompt_type_id_seq OWNED BY public.prompt_types.prompt_type_id;
CREATE TABLE public.run_evaluations (
    run_id integer NOT NULL,
    test_id integer NOT NULL,
    judge_model_id integer NOT NULL,
    judge_run_index integer NOT NULL,
    response_quality_label text,
    response_quality_score numeric(4,2),
    citation_quality_label text,
    citation_quality_score numeric(4,2)
);
CREATE TABLE public.run_results (
    run_id integer NOT NULL,
    test_id integer NOT NULL,
    raw_answer text NOT NULL,
    raw_citations jsonb
);
CREATE TABLE public.runs (
    run_id integer NOT NULL,
    organization_id integer NOT NULL,
    perimeter_id integer,
    tested_model_id integer NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    run_meta jsonb
);
CREATE SEQUENCE public.runs_run_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.runs_run_id_seq OWNED BY public.runs.run_id;
CREATE TABLE public.scheduled_runs (
    schedule_id integer NOT NULL,
    organization_id integer NOT NULL,
    perimeter_id integer NOT NULL,
    name text NOT NULL,
    tested_models jsonb NOT NULL,
    judges jsonb NOT NULL,
    test_ids jsonb,
    note text,
    schedule_kind text NOT NULL,
    schedule_config jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    next_run_at timestamp with time zone,
    last_run_at timestamp with time zone,
    last_job_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.scheduled_runs_schedule_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.scheduled_runs_schedule_id_seq OWNED BY public.scheduled_runs.schedule_id;
CREATE TABLE public.test_ground_truth (
    id integer NOT NULL,
    test_id integer NOT NULL,
    version integer NOT NULL,
    reference_answer text NOT NULL,
    reference_urls jsonb,
    valid_from timestamp with time zone DEFAULT now() NOT NULL,
    valid_to timestamp with time zone,
    created_by integer,
    notes text
);
CREATE SEQUENCE public.test_ground_truth_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.test_ground_truth_id_seq OWNED BY public.test_ground_truth.id;
CREATE TABLE public.tests (
    test_id integer NOT NULL,
    organization_id integer NOT NULL,
    perimeter_id integer NOT NULL,
    prompt text NOT NULL,
    expected_answer text,
    response_quality_prompt_id integer,
    citation_quality_prompt_id integer,
    validity_start_at timestamp with time zone NOT NULL,
    validity_end_at timestamp with time zone
);
CREATE SEQUENCE public.tests_test_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.tests_test_id_seq OWNED BY public.tests.test_id;
CREATE TABLE public.usage (
    id integer NOT NULL,
    organization_id integer NOT NULL,
    model_id integer NOT NULL,
    run_id integer,
    kind text NOT NULL,
    billed_to text NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    cost_eur numeric(12,6) DEFAULT '0'::numeric NOT NULL,
    cost_usd numeric(12,6)
);
CREATE SEQUENCE public.usage_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.usage_id_seq OWNED BY public.usage.id;
CREATE TABLE public.users (
    id integer NOT NULL,
    email text NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone,
    is_superuser_cached boolean DEFAULT false NOT NULL,
    password_hash text,
    auth_provider text DEFAULT 'local'::text NOT NULL,
    oidc_issuer text,
    oidc_external_id text,
    is_platform_admin boolean DEFAULT false NOT NULL
);
CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;
ALTER TABLE ONLY public.api_tokens ALTER COLUMN id SET DEFAULT nextval('public.api_tokens_id_seq'::regclass);
ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);
ALTER TABLE ONLY public.auth_tokens ALTER COLUMN id SET DEFAULT nextval('public.auth_tokens_id_seq'::regclass);
ALTER TABLE ONLY public.evaluation_prompts ALTER COLUMN prompt_id SET DEFAULT nextval('public.evaluation_prompts_prompt_id_seq'::regclass);
ALTER TABLE ONLY public.gold_annotations ALTER COLUMN id SET DEFAULT nextval('public.gold_annotations_id_seq'::regclass);
ALTER TABLE ONLY public.invitations ALTER COLUMN id SET DEFAULT nextval('public.invitations_id_seq'::regclass);
ALTER TABLE ONLY public.job_logs ALTER COLUMN id SET DEFAULT nextval('public.job_logs_id_seq'::regclass);
ALTER TABLE ONLY public.model_pricing ALTER COLUMN id SET DEFAULT nextval('public.model_pricing_id_seq'::regclass);
ALTER TABLE ONLY public.models ALTER COLUMN model_id SET DEFAULT nextval('public.models_model_id_seq'::regclass);
ALTER TABLE ONLY public.org_credentials ALTER COLUMN id SET DEFAULT nextval('public.org_credentials_id_seq'::regclass);
ALTER TABLE ONLY public.org_models ALTER COLUMN id SET DEFAULT nextval('public.org_models_id_seq'::regclass);
ALTER TABLE ONLY public.organizations ALTER COLUMN id SET DEFAULT nextval('public.organizations_id_seq'::regclass);
ALTER TABLE ONLY public.perimeters ALTER COLUMN id SET DEFAULT nextval('public.perimeters_id_seq'::regclass);
ALTER TABLE ONLY public.prompt_types ALTER COLUMN prompt_type_id SET DEFAULT nextval('public.prompt_types_prompt_type_id_seq'::regclass);
ALTER TABLE ONLY public.runs ALTER COLUMN run_id SET DEFAULT nextval('public.runs_run_id_seq'::regclass);
ALTER TABLE ONLY public.scheduled_runs ALTER COLUMN schedule_id SET DEFAULT nextval('public.scheduled_runs_schedule_id_seq'::regclass);
ALTER TABLE ONLY public.test_ground_truth ALTER COLUMN id SET DEFAULT nextval('public.test_ground_truth_id_seq'::regclass);
ALTER TABLE ONLY public.tests ALTER COLUMN test_id SET DEFAULT nextval('public.tests_test_id_seq'::regclass);
ALTER TABLE ONLY public.usage ALTER COLUMN id SET DEFAULT nextval('public.usage_id_seq'::regclass);
ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);
ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_token_hash_key UNIQUE (token_hash);
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT auth_tokens_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT auth_tokens_token_key UNIQUE (token);
ALTER TABLE ONLY public.budgets
    ADD CONSTRAINT budgets_pkey PRIMARY KEY (organization_id);
ALTER TABLE ONLY public.evaluation_prompts
    ADD CONSTRAINT evaluation_prompts_pkey PRIMARY KEY (prompt_id);
ALTER TABLE ONLY public.gold_annotations
    ADD CONSTRAINT gold_annotations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_token_key UNIQUE (token);
ALTER TABLE ONLY public.job_logs
    ADD CONSTRAINT job_logs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_pkey PRIMARY KEY (user_id, org_id);
ALTER TABLE ONLY public.model_pricing
    ADD CONSTRAINT model_pricing_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.models
    ADD CONSTRAINT models_pkey PRIMARY KEY (model_id);
ALTER TABLE ONLY public.org_credentials
    ADD CONSTRAINT org_credentials_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.org_models
    ADD CONSTRAINT org_models_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_slug_key UNIQUE (slug);
ALTER TABLE ONLY public.perimeters
    ADD CONSTRAINT perimeters_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.prompt_types
    ADD CONSTRAINT prompt_types_pkey PRIMARY KEY (prompt_type_id);
ALTER TABLE ONLY public.run_evaluations
    ADD CONSTRAINT run_evaluations_pkey PRIMARY KEY (run_id, test_id, judge_model_id, judge_run_index);
ALTER TABLE ONLY public.run_results
    ADD CONSTRAINT run_results_pkey PRIMARY KEY (run_id, test_id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_pkey PRIMARY KEY (run_id);
ALTER TABLE ONLY public.scheduled_runs
    ADD CONSTRAINT scheduled_runs_pkey PRIMARY KEY (schedule_id);
ALTER TABLE ONLY public.test_ground_truth
    ADD CONSTRAINT test_ground_truth_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tests
    ADD CONSTRAINT tests_pkey PRIMARY KEY (test_id);
ALTER TABLE ONLY public.org_models
    ADD CONSTRAINT uq_org_models_org_model UNIQUE (organization_id, model_id);
ALTER TABLE ONLY public.usage
    ADD CONSTRAINT usage_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);
CREATE INDEX ix_api_tokens_organization_id ON public.api_tokens USING btree (organization_id);
CREATE INDEX ix_audit_log_org_at ON public.audit_log USING btree (org_id, at DESC);
CREATE INDEX ix_audit_log_user_at ON public.audit_log USING btree (user_id, at DESC);
CREATE INDEX ix_auth_tokens_user_id ON public.auth_tokens USING btree (user_id);
CREATE INDEX ix_gold_annotations_run_id ON public.gold_annotations USING btree (run_id);
CREATE INDEX ix_invitations_email ON public.invitations USING btree (email);
CREATE INDEX ix_invitations_org_id ON public.invitations USING btree (org_id);
CREATE INDEX ix_job_logs_job_id ON public.job_logs USING btree (job_id);
CREATE INDEX ix_jobs_organization_id ON public.jobs USING btree (organization_id);
CREATE INDEX ix_jobs_queue ON public.jobs USING btree (status, priority DESC, created_at);
CREATE INDEX ix_model_pricing_active ON public.model_pricing USING btree (model_id) WHERE (effective_to IS NULL);
CREATE INDEX ix_org_models_org_id ON public.org_models USING btree (organization_id);
CREATE INDEX ix_perimeters_org_id ON public.perimeters USING btree (organization_id);
CREATE INDEX ix_runs_organization_id ON public.runs USING btree (organization_id);
CREATE INDEX ix_runs_perimeter_id ON public.runs USING btree (perimeter_id);
CREATE INDEX ix_scheduled_runs_organization_id ON public.scheduled_runs USING btree (organization_id);
CREATE INDEX ix_scheduled_runs_perimeter_id ON public.scheduled_runs USING btree (perimeter_id);
CREATE INDEX ix_test_ground_truth_active ON public.test_ground_truth USING btree (test_id) WHERE (valid_to IS NULL);
CREATE INDEX ix_tests_organization_id ON public.tests USING btree (organization_id);
CREATE INDEX ix_tests_perimeter_id ON public.tests USING btree (perimeter_id);
CREATE INDEX ix_usage_org_ts ON public.usage USING btree (organization_id, ts DESC);
CREATE INDEX ix_usage_run_id ON public.usage USING btree (run_id);
CREATE UNIQUE INDEX uq_gold_annotations_test_run_annotator ON public.gold_annotations USING btree (test_id, run_id, annotator_email);
CREATE UNIQUE INDEX uq_org_credentials_org_model ON public.org_credentials USING btree (organization_id, model_id);
CREATE UNIQUE INDEX uq_perimeters_org_slug ON public.perimeters USING btree (organization_id, slug);
CREATE UNIQUE INDEX uq_users_oidc_identity ON public.users USING btree (oidc_issuer, oidc_external_id) WHERE ((oidc_issuer IS NOT NULL) AND (oidc_external_id IS NOT NULL));
ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.api_tokens
    ADD CONSTRAINT api_tokens_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT auth_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.budgets
    ADD CONSTRAINT budgets_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.budgets
    ADD CONSTRAINT budgets_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.evaluation_prompts
    ADD CONSTRAINT evaluation_prompts_prompt_type_id_fkey FOREIGN KEY (prompt_type_id) REFERENCES public.prompt_types(prompt_type_id);
ALTER TABLE ONLY public.gold_annotations
    ADD CONSTRAINT gold_annotations_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(run_id);
ALTER TABLE ONLY public.gold_annotations
    ADD CONSTRAINT gold_annotations_test_id_fkey FOREIGN KEY (test_id) REFERENCES public.tests(test_id);
ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.job_logs
    ADD CONSTRAINT job_logs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.model_pricing
    ADD CONSTRAINT model_pricing_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(model_id);
ALTER TABLE ONLY public.org_credentials
    ADD CONSTRAINT org_credentials_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(model_id);
ALTER TABLE ONLY public.org_credentials
    ADD CONSTRAINT org_credentials_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.org_models
    ADD CONSTRAINT org_models_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(model_id);
ALTER TABLE ONLY public.org_models
    ADD CONSTRAINT org_models_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.perimeters
    ADD CONSTRAINT perimeters_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.perimeters
    ADD CONSTRAINT perimeters_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.run_evaluations
    ADD CONSTRAINT run_evaluations_judge_model_id_fkey FOREIGN KEY (judge_model_id) REFERENCES public.models(model_id);
ALTER TABLE ONLY public.run_evaluations
    ADD CONSTRAINT run_evaluations_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(run_id);
ALTER TABLE ONLY public.run_evaluations
    ADD CONSTRAINT run_evaluations_test_id_fkey FOREIGN KEY (test_id) REFERENCES public.tests(test_id);
ALTER TABLE ONLY public.run_results
    ADD CONSTRAINT run_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(run_id);
ALTER TABLE ONLY public.run_results
    ADD CONSTRAINT run_results_test_id_fkey FOREIGN KEY (test_id) REFERENCES public.tests(test_id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_perimeter_id_fkey FOREIGN KEY (perimeter_id) REFERENCES public.perimeters(id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_tested_model_id_fkey FOREIGN KEY (tested_model_id) REFERENCES public.models(model_id);
ALTER TABLE ONLY public.scheduled_runs
    ADD CONSTRAINT scheduled_runs_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.scheduled_runs
    ADD CONSTRAINT scheduled_runs_perimeter_id_fkey FOREIGN KEY (perimeter_id) REFERENCES public.perimeters(id);
ALTER TABLE ONLY public.test_ground_truth
    ADD CONSTRAINT test_ground_truth_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.test_ground_truth
    ADD CONSTRAINT test_ground_truth_test_id_fkey FOREIGN KEY (test_id) REFERENCES public.tests(test_id);
ALTER TABLE ONLY public.tests
    ADD CONSTRAINT tests_citation_quality_prompt_id_fkey FOREIGN KEY (citation_quality_prompt_id) REFERENCES public.evaluation_prompts(prompt_id);
ALTER TABLE ONLY public.tests
    ADD CONSTRAINT tests_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.tests
    ADD CONSTRAINT tests_perimeter_id_fkey FOREIGN KEY (perimeter_id) REFERENCES public.perimeters(id);
ALTER TABLE ONLY public.tests
    ADD CONSTRAINT tests_response_quality_prompt_id_fkey FOREIGN KEY (response_quality_prompt_id) REFERENCES public.evaluation_prompts(prompt_id);
ALTER TABLE ONLY public.usage
    ADD CONSTRAINT usage_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.models(model_id);
ALTER TABLE ONLY public.usage
    ADD CONSTRAINT usage_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES public.organizations(id);
ALTER TABLE ONLY public.usage
    ADD CONSTRAINT usage_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.runs(run_id);
