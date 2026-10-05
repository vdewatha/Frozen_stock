import { type ReactNode } from 'react';
import { ClerkProvider, SignIn, SignUp } from '@clerk/react';
import { publishableKeyFromHost } from '@clerk/react/internal';
import { shadcn } from '@clerk/themes';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import DashboardHome from '@/pages/home';
import {
  Route,
  Switch,
  useLocation,
  Router as WouterRouter,
} from 'wouter';

const queryClient = new QueryClient();
const basePath = import.meta.env.BASE_URL.replace(/\/$/, '');
const localPaperAuth = import.meta.env.VITE_LOCAL_PAPER_AUTH === 'true';
const clerkPubKey = localPaperAuth ? '' : publishableKeyFromHost(
  window.location.hostname,
  import.meta.env.VITE_CLERK_PUBLISHABLE_KEY,
);
const clerkProxyUrl = import.meta.env.VITE_CLERK_PROXY_URL;

function stripBase(path: string): string {
  return basePath && path.startsWith(basePath)
    ? path.slice(basePath.length) || '/'
    : path;
}

const clerkAppearance = {
  theme: shadcn,
  options: {
    logoPlacement: 'inside' as const,
    logoLinkUrl: basePath || '/',
    logoImageUrl: `${window.location.origin}${basePath}/logo.svg`,
  },
  variables: {
    colorPrimary: '#0f8b6f',
    colorForeground: '#111827',
    colorMutedForeground: '#64748b',
    colorDanger: '#b91c1c',
    colorBackground: '#ffffff',
    colorInput: '#f8fafc',
    colorInputForeground: '#111827',
    colorNeutral: '#cbd5e1',
    fontFamily: 'Arial, Helvetica, sans-serif',
    borderRadius: '0.5rem',
  },
  elements: {
    rootBox: { width: '100%', display: 'flex', justifyContent: 'center' },
    cardBox: {
      width: '440px',
      maxWidth: '100%',
      overflow: 'hidden',
      border: '1px solid #d8dee8',
      boxShadow: '0 18px 50px rgba(15, 23, 42, 0.10)',
    },
    headerTitle: { color: '#111827', fontWeight: 700 },
    headerSubtitle: { color: '#64748b' },
    formButtonPrimary: { backgroundColor: '#0f8b6f' },
    footerActionLink: { color: '#0f8b6f', fontWeight: 600 },
  },
};

function SignInPage() {
  return (
    <main className="flex min-h-[100dvh] items-center justify-center bg-[#eef1f5] px-4 py-10">
      <SignIn
        routing="path"
        path={`${basePath}/sign-in`}
        signUpUrl={`${basePath}/sign-up`}
        fallbackRedirectUrl={basePath || '/'}
      />
    </main>
  );
}

function SignUpPage() {
  return (
    <main className="flex min-h-[100dvh] items-center justify-center bg-[#eef1f5] px-4 py-10">
      <SignUp
        routing="path"
        path={`${basePath}/sign-up`}
        signInUrl={`${basePath}/sign-in`}
        fallbackRedirectUrl={basePath || '/'}
      />
    </main>
  );
}

function Router() {
  return (
    <RoutedErrorBoundary>
      <Switch>
        <Route path="/" component={DashboardHome} />
        <Route path="/sign-in/*?" component={localPaperAuth ? DashboardHome : SignInPage} />
        <Route path="/sign-up/*?" component={localPaperAuth ? DashboardHome : SignUpPage} />
        <Route path="/:page/:section?" component={DashboardHome} />
        <Route component={NotFound} />
      </Switch>
    </RoutedErrorBoundary>
  );
}

function RoutedErrorBoundary({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  return <ErrorBoundary resetKey={location}>{children}</ErrorBoundary>;
}

function App() {
  return (
    <WouterRouter base={basePath}>
      {localPaperAuth ? <ApplicationContent /> : <ClerkProviderWithRouting />}
    </WouterRouter>
  );
}

function ClerkProviderWithRouting() {
  const [, setLocation] = useLocation();
  return (
    <ClerkProvider
      publishableKey={clerkPubKey}
      proxyUrl={clerkProxyUrl}
      appearance={clerkAppearance}
      signInUrl={`${basePath}/sign-in`}
      signUpUrl={`${basePath}/sign-up`}
      localization={{
        signIn: { start: { title: 'Strategy Control Room', subtitle: 'Sign in to review paper-trading evidence' } },
        signUp: { start: { title: 'Request control-room access', subtitle: 'New accounts receive read-only access by default' } },
      }}
      routerPush={(to) => setLocation(stripBase(to))}
      routerReplace={(to) => setLocation(stripBase(to), { replace: true })}
    >
      <ApplicationContent />
    </ClerkProvider>
  );
}

export default App;

function ApplicationContent() {
  return <QueryClientProvider client={queryClient}>
    <TooltipProvider><Router /><Toaster /></TooltipProvider>
  </QueryClientProvider>;
}
