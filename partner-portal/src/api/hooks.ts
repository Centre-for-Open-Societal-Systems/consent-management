// Shared react-query hooks.
import { useQuery } from "@tanstack/react-query";
import { api, listUseCases } from "./client";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 5 * 60_000 });
}

export function useBindings() {
  return useQuery({ queryKey: ["bindings"], queryFn: api.bindings, staleTime: 60_000 });
}

export function useSubjectAuthConfig() {
  return useQuery({ queryKey: ["subject-auth-config"], queryFn: api.subjectAuthConfig, staleTime: 5 * 60_000 });
}

export function useRequestSettings() {
  return useQuery({ queryKey: ["request-settings"], queryFn: api.requestSettings, staleTime: 5 * 60_000 });
}

export function useUseCases(enabled = true) {
  return useQuery({ queryKey: ["use-cases"], queryFn: listUseCases, staleTime: 5 * 60_000, enabled });
}
