export const cachePolicies = {
  reference: { staleTime: 30 * 60_000, gcTime: 2 * 60 * 60_000 },
  aggregate: { staleTime: 5 * 60_000, gcTime: 30 * 60_000 },
  requestList: { staleTime: 2 * 60_000, gcTime: 30 * 60_000 },
  requestDetail: { staleTime: 30 * 60_000, gcTime: 60 * 60_000 },
} as const
