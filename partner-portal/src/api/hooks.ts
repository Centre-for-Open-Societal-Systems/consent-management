// Shared react-query hooks.
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 5 * 60_000 });
}

export function useAccess() {
  return useQuery({ queryKey: ["access"], queryFn: api.access, staleTime: 60_000 });
}

export function useSubjectAuthConfig() {
  return useQuery({ queryKey: ["subject-auth-config"], queryFn: api.subjectAuthConfig, staleTime: 5 * 60_000 });
}

export function useRequestSettings() {
  return useQuery({ queryKey: ["request-settings"], queryFn: api.requestSettings, staleTime: 5 * 60_000 });
}
