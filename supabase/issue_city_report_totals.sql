-- Run once in Supabase SQL Editor to enable all-time city rankings in the chatbot.
-- This is a live view over hourly_issue_counts, so hourly Gold upserts are reflected
-- automatically. Apply the same SELECT access policy used by the app on the source table.
CREATE OR REPLACE VIEW public.issue_city_report_totals
WITH (security_invoker = true)
AS
SELECT
    city,
    issue_type,
    SUM(n_reports)::bigint AS total_reports,
    MIN(hour_utc) AS first_report_utc,
    MAX(hour_utc) AS last_report_utc,
    COUNT(*)::bigint AS observed_hours
FROM public.hourly_issue_counts
WHERE city IS NOT NULL
  AND issue_type IS NOT NULL
  AND city IN (
      'Karachi', 'Lahore', 'Islamabad', 'Rawalpindi', 'Faisalabad', 'Multan',
      'Peshawar', 'Quetta', 'Hyderabad', 'Gujranwala', 'Sialkot', 'Sukkur',
      'Larkana', 'Bahawalpur', 'Sargodha', 'Abbottabad', 'Mardan', 'Gwadar'
  )
GROUP BY city, issue_type;

GRANT SELECT ON public.issue_city_report_totals TO anon, authenticated;

-- One row per configured city, issue group, and Pakistan-local calendar day.
-- This supports date-specific chatbot answers without scanning hourly rows.
CREATE OR REPLACE VIEW public.issue_city_report_daily
WITH (security_invoker = true)
AS
SELECT
    city,
    issue_type,
    (hour_utc AT TIME ZONE 'Asia/Karachi')::date AS report_date,
    SUM(n_reports)::bigint AS total_reports,
    COUNT(*)::bigint AS observed_hours
FROM public.hourly_issue_counts
WHERE city IN (
      'Karachi', 'Lahore', 'Islamabad', 'Rawalpindi', 'Faisalabad', 'Multan',
      'Peshawar', 'Quetta', 'Hyderabad', 'Gujranwala', 'Sialkot', 'Sukkur',
      'Larkana', 'Bahawalpur', 'Sargodha', 'Abbottabad', 'Mardan', 'Gwadar'
  )
  AND issue_type IS NOT NULL
GROUP BY city, issue_type, (hour_utc AT TIME ZONE 'Asia/Karachi')::date;

GRANT SELECT ON public.issue_city_report_daily TO anon, authenticated;
