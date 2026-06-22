# Registration confirmation email delivery

The `/api/v1/auth/register` endpoint uses Supabase Auth for email/password registration. If Supabase is configured to require email confirmation, Supabase sends the confirmation message during `/auth/v1/signup`. A response such as `{"detail":"Error sending confirmation email"}` means registration reached Supabase, but Supabase could not deliver that confirmation email.

## Production root cause checklist

Check the Supabase project **Authentication > Emails / SMTP** settings, not only the Render service variables:

- SMTP host is present and matches the provider.
- SMTP port matches the encryption mode: use `587` for STARTTLS or `465` for SSL.
- SMTP username is the provider's SMTP login, not always the sender email.
- SMTP password is an API key/app password, not a normal account password for providers that require app credentials.
- The `From` email/domain is verified with the provider.
- Gmail accounts use a 16-character app password and 2FA; normal Gmail passwords are rejected.
- Brevo/SendGrid/Resend accounts have SMTP enabled and are not blocked by sender/domain verification or free-tier sending limits.

## Recommended Render environment variables

These application variables are still useful for app-owned email features, but Supabase confirmation email delivery is configured in the Supabase dashboard:

```env
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_KEY=<supabase-anon-key>
SUPABASE_SERVICE_ROLE_KEY=<supabase-service-role-key>

# App SMTP settings, if/when the backend sends mail directly
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USER=<brevo-smtp-login>
SMTP_PASS=<brevo-smtp-key>
EMAIL_FROM=VEDAAPEX <no-reply@your-verified-domain.com>
```

## Provider recommendation

For production, prefer Brevo, Resend, or SendGrid with a verified sending domain. Brevo is a practical free-tier SMTP option for early production because it provides SMTP credentials and daily free sending. Avoid personal Gmail for production; it is fragile, rate-limited, and requires app passwords.
