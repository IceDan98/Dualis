# utils/token_counter.py
import logging
import re
from typing import Union, List, Dict, Optional, Callable 
import asyncio 

# For precise token count for Gemini, use the Google API client library
try:
    import google.generativeai as genai
except ImportError:
    genai = None
    logging.warning(
        "google-generativeai library not installed. "
        "Precise token counting for Gemini will not be available. "
        "Install with: pip install google-generativeai"
    )

logger = logging.getLogger(__name__)

class TokenCounter:
    """
    Service for approximate and potentially precise token counting for Large Language Models (LLMs)
    like Gemini and OpenAI.

    The estimation is based on character-to-token ratios, which can vary.
    For Gemini, it's recommended to use the official API/SDK for precise counting when possible.
    """
    
    def __init__(self, 
                 gemini_api_key: Optional[str] = None, 
                 gemini_model_name_for_counting: str = "gemini-2.0-flash"): # Updated default model
        """
        Initializes TokenCounter.
        Args:
            gemini_api_key (Optional[str]): API key for Gemini, if direct API calls for token counting are to be made.
            gemini_model_name_for_counting (str): The specific Gemini model name to use for token counting.
                                                  Default is "gemini-2.0-flash".
        """
        self.gemini_api_key = gemini_api_key
        self.gemini_model_name_for_counting = gemini_model_name_for_counting
        self._gemini_model_instance = None

        if genai and self.gemini_api_key:
            try:
                # Configure the API key globally for the genai library if not already done.
                # This step might be done once at the application startup.
                # If genai.configure() is called multiple times with the same key, it's usually fine.
                genai.configure(api_key=self.gemini_api_key)
                self._gemini_model_instance = genai.GenerativeModel(self.gemini_model_name_for_counting)
                logger.info(f"Gemini model '{self.gemini_model_name_for_counting}' initialized for token counting.")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini model '{self.gemini_model_name_for_counting}' for token counting: {e}. Will use approximation.", exc_info=True)
                self._gemini_model_instance = None
        elif not genai:
            logger.info("google-generativeai SDK not available. Using approximation for Gemini token counting.")
        elif not self.gemini_api_key:
            logger.info("Gemini API key not provided. Using approximation for Gemini token counting.")


        # Approximate character-to-token ratios (fallback or for non-Gemini models)
        self.char_to_token_ratios: Dict[str, Dict[str, float]] = {
            'gemini_approx': { # Ratios for approximate counting if API is not used
                'russian': 0.3, # Example: ~1 token per 3.3 chars for Russian
                'english': 0.25, # Example: ~1 token per 4 chars for English
                'mixed': 0.28    # For mixed Russian-English content
            },
            'openai': { # Example for GPT models
                'russian': 0.35, 
                'english': 0.25,
                'mixed': 0.3
            }
            # Other models can be added here
        }
        # Default for unknown models or when precise counting fails
        self.default_char_to_token_ratio = 0.3 

    async def _count_tokens_gemini_sdk(self, text: str) -> Optional[int]:
        """
        Counts tokens using the Gemini SDK (genai.GenerativeModel.count_tokens).
        """
        if not self._gemini_model_instance:
            logger.debug(f"Gemini model instance for '{self.gemini_model_name_for_counting}' not available for SDK token counting.")
            return None
        
        try:
            # The SDK's count_tokens might be synchronous.
            # Running in a thread to avoid blocking the asyncio event loop.
            # The genai.GenerativeModel.count_tokens method itself is synchronous.
            response = await asyncio.to_thread(self._gemini_model_instance.count_tokens, text)
            
            if hasattr(response, 'total_tokens'):
                return response.total_tokens # type: ignore
            else:
                logger.error(f"Unexpected response structure from Gemini SDK count_tokens for model '{self.gemini_model_name_for_counting}': {response}")
                return None
        except Exception as e:
            logger.error(f"Error counting tokens with Gemini SDK for model '{self.gemini_model_name_for_counting}': {e}", exc_info=True)
            # Potentially, if the error is specific (e.g., API key issue), we might want to disable further SDK attempts.
            # For now, it will just fallback to approximation on the next call to count_tokens if this fails.
            return None

    async def count_tokens(self, text: str, model: str = 'gemini', language: str = 'mixed') -> int:
        """
        Counts the approximate or precise number of tokens in the provided text.
        For 'gemini', it will attempt to use the SDK if configured, otherwise falls back to approximation.
        """
        if not text: # Handle empty string explicitly
            return 0
        
        # Use the model name configured for counting if 'gemini' is requested
        # and an SDK instance is available.
        if model == 'gemini' and self._gemini_model_instance:
            precise_count = await self._count_tokens_gemini_sdk(text)
            if precise_count is not None:
                logger.debug(f"Precisely counted {precise_count} tokens for Gemini model '{self.gemini_model_name_for_counting}' (SDK) for text starting with: '{text[:30]}...'")
                return precise_count
            # Fallback to approximation if SDK call fails
            logger.debug(f"Falling back to approximate token counting for Gemini model '{self.gemini_model_name_for_counting}' after SDK attempt failed.")
            model_key_for_ratios = 'gemini_approx' 
        elif model == 'gemini': # SDK not available or not configured for 'gemini'
             logger.debug(f"Using approximate token counting for Gemini model (SDK not available/configured).")
             model_key_for_ratios = 'gemini_approx'
        else: # For other models like 'openai'
            model_key_for_ratios = model

        # Approximate counting logic
        try:
            effective_language = language
            if language == 'mixed' or not language: # Added check for empty language string
                effective_language = self._detect_language(text)
            
            model_ratios_map = self.char_to_token_ratios.get(model_key_for_ratios, {})
            ratio = model_ratios_map.get(effective_language, 
                                         model_ratios_map.get('mixed', self.default_char_to_token_ratio))
            
            char_count = len(text)
            estimated_tokens = int(char_count * ratio)
            
            # Apply corrections if using approximation
            if model_key_for_ratios.endswith('_approx') or model_key_for_ratios not in ['gemini']: # Apply corrections for approximations
                 estimated_tokens = self._apply_corrections(text, estimated_tokens, effective_language)
            
            logger.debug(f"Approximated {estimated_tokens} tokens for model '{model_key_for_ratios}', lang '{effective_language}' for text: '{text[:30]}...'")
            return max(1, estimated_tokens) if text else 0 # Ensure 0 for empty text
            
        except Exception as e:
            logger.error(f"Error in approximate token counting for model '{model_key_for_ratios}': {e}. Text (start): '{text[:100]}'", exc_info=True)
            # Fallback to a very rough estimate if approximation fails
            return max(1, len(text) // 4) if text else 0

    def _detect_language(self, text: str) -> str:
        """
        Automatically detects the predominant language in the text (Russian or English).
        """
        if not text:
            return 'mixed'

        cyrillic_count = len(re.findall(r'[а-яА-ЯёЁ]', text))
        latin_count = len(re.findall(r'[a-zA-Z]', text))
        
        total_letters = cyrillic_count + latin_count
        
        if total_letters == 0: # If no letters (e.g., only numbers or symbols)
            return 'mixed' 
        
        cyrillic_ratio = cyrillic_count / total_letters
        
        # Thresholds can be adjusted
        if cyrillic_ratio > 0.7:
            return 'russian'
        elif cyrillic_ratio < 0.3: # equivalent to latin_count / total_letters > 0.7
            return 'english'
        else:
            return 'mixed'
    
    def _apply_corrections(self, text: str, base_estimate: int, language: str) -> int:
        """
        Applies corrections to the base token estimate (used for approximate counting).
        """
        corrected_estimate = base_estimate
        
        # Correction for special characters
        special_chars_count = len(re.findall(r'[^\w\s]', text))
        if special_chars_count > len(text) * 0.05: # If more than 5% special chars
            corrected_estimate = int(corrected_estimate * 1.05 + special_chars_count * 0.5)

        # Correction for short words
        words = re.findall(r'\b\w+\b', text) # Find all words
        if words: 
            short_words_count = sum(1 for word in words if len(word) <= 3)
            if language == 'russian' and short_words_count > len(words) * 0.25: 
                corrected_estimate = int(corrected_estimate * 1.03) 
            elif language == 'english' and short_words_count > len(words) * 0.35: 
                corrected_estimate = int(corrected_estimate * 1.02)
        
        # Correction for code-like structures or many newlines
        if '\n' in text and len(re.findall(r'\n\s*', text)) > 5: # If many newlines/indentations
             corrected_estimate = int(corrected_estimate * 1.02)

        return max(0, corrected_estimate) # Tokens cannot be negative
    
    async def count_tokens_in_messages(self, messages: List[Dict[str, str]], model: str = 'gemini') -> int:
        """
        Counts the total approximate/precise tokens in a list of messages.
        Each message is a dict with 'role' and 'content'.
        """
        total_tokens = 0
        # Overhead per message (approximate, varies by model and formatting)
        # For Gemini, the SDK's count_tokens on the full content (if formatted correctly)
        # might handle this implicitly. If counting part by part, overhead is needed.
        tokens_per_message_overhead = 3 # A general estimate for non-SDK counting
        
        # If using SDK for precise count, it's better to format the whole message list
        # according to Gemini's schema and count tokens for that entire structure if the API supports it.
        # Since we are counting message by message here, we add overhead.
        
        for msg in messages:
            content = msg.get('content', '')
            role = msg.get('role', '') # Role itself also consumes tokens
            
            content_tokens = await self.count_tokens(content, model)
            total_tokens += content_tokens
            
            if role: # Role text is usually short
                role_tokens = await self.count_tokens(role, model) 
                total_tokens += role_tokens
            
            total_tokens += tokens_per_message_overhead
        
        return total_tokens
    
    async def _truncate_message_content(self, content: str, max_tokens: int, model: str) -> Optional[str]:
        """
        Truncates message content to a specified token limit.
        Attempts to truncate by sentences, then words.
        Uses the main count_tokens method for checking.
        """
        if not content: return "" # Return empty string if content was empty
        if max_tokens < 1: return "..." if content else "" # Minimal truncation if max_tokens is too low

        current_tokens = await self.count_tokens(content, model)
        if current_tokens <= max_tokens:
            return content

        # Attempt truncation by sentences
        sentences = re.split(r'(?<=[.!?])\s+', content.strip())
        if not sentences: sentences = [content] # Treat as one sentence if no delimiters

        # Try building from sentences
        if len(sentences) > 1:
            temp_content_sentences = ""
            for i, sentence_part in enumerate(sentences):
                # Check if adding the next sentence (with potential "...") exceeds the limit
                prospective_addition = (temp_content_sentences + (" " if temp_content_sentences else "") + sentence_part).strip()
                # Add ellipsis for checking if it's not the full content being tested
                suffix_for_check = "..." if prospective_addition != content.strip() else ""
                
                tokens_with_next_sentence = await self.count_tokens(prospective_addition + suffix_for_check, model)

                if tokens_with_next_sentence <= max_tokens:
                    temp_content_sentences = prospective_addition
                else:
                    # Adding this sentence_part makes it too long.
                    # Use the previously accumulated temp_content_sentences.
                    if temp_content_sentences: # If we have some content that fits
                        return temp_content_sentences + "..." # Add ellipsis
                    else: # The very first sentence is already too long
                        break # Break to word-level truncation for the first sentence
            
            # If all sentences fit or some sentences fit and we exited the loop
            if temp_content_sentences:
                 # Final check for the accumulated sentences
                if (await self.count_tokens(temp_content_sentences + ("..." if temp_content_sentences != content.strip() else ""), model)) <= max_tokens:
                    return temp_content_sentences + ("..." if temp_content_sentences != content.strip() else "")
                elif (await self.count_tokens(temp_content_sentences, model)) <= max_tokens: # If it fits without ellipsis
                     return temp_content_sentences


        # Fallback to word-level truncation if sentence truncation was not sufficient or applicable
        words = content.split()
        
        # Binary search for the right number of words (more efficient for long texts)
        low = 0
        high = len(words)
        best_fit_word_list: List[str] = []

        while low <= high:
            mid = (low + high) // 2
            if mid == 0: # Avoid empty string if possible, unless max_tokens is extremely small
                current_text_segment = ""
                current_segment_tokens = 0
                if max_tokens > 0 : # If we can't even fit one word, this will result in empty
                    low = mid + 1 # Try to include at least one word if possible
                    continue
            else:
                current_text_segment = " ".join(words[:mid])
                current_segment_tokens = await self.count_tokens(current_text_segment + "...", model)

            if current_segment_tokens <= max_tokens:
                best_fit_word_list = words[:mid]
                low = mid + 1
            else:
                high = mid - 1
        
        if best_fit_word_list:
            final_text = " ".join(best_fit_word_list)
            # Check if it's the full content or if ellipsis is needed
            if final_text.strip() != content.strip():
                return final_text + "..."
            else:
                return final_text # No ellipsis if it's the full content that fits

        logger.warning(f"Aggressive truncation needed or failed for content to {max_tokens} tokens. Original: {current_tokens} tokens. Content: '{content[:50]}...'")
        # Last resort: very rough character-based truncation if word/sentence failed.
        # This is a very crude fallback.
        if self.default_char_to_token_ratio > 0:
            # Estimate characters, take a bit less for safety and ellipsis
            estimated_chars = int((max_tokens - await self.count_tokens("...", model)) / self.default_char_to_token_ratio * 0.9) 
            if estimated_chars > 10 : # Ensure some meaningful content
                 return content[:estimated_chars] + "..."
        
        # Absolute fallback if all else fails (e.g., max_tokens is very small)
        return content[:max(1, max_tokens * 2)] + "..." # Max 1 char or roughly 2 chars per token + ellipsis


    async def optimize_context_for_limit(self,
                                 messages: List[Dict[str, str]],
                                 token_limit: int,
                                 model: str = 'gemini'
                                 ) -> List[Dict[str, str]]:
        """
        Optimizes a list of messages (dialogue context) to not exceed a token limit.
        """
        if not messages:
            return []

        # Calculate initial total tokens
        current_total_tokens = await self.count_tokens_in_messages(messages, model)

        if current_total_tokens <= token_limit:
            return messages # No optimization needed

        logger.info(f"Optimizing context: {current_total_tokens} tokens -> target limit {token_limit} tokens (model: {model})")

        system_messages = [msg for msg in messages if msg.get('role') == 'system']
        dialogue_messages = [msg for msg in messages if msg.get('role') != 'system']

        # Calculate tokens for system messages
        tokens_for_system_messages = await self.count_tokens_in_messages(system_messages, model)
        
        # Available tokens for dialogue messages
        remaining_token_limit_for_dialogue = token_limit - tokens_for_system_messages

        if remaining_token_limit_for_dialogue < 0:
            # This means system messages alone exceed the total limit.
            # This is a critical issue. We might try to truncate system messages or return an error.
            # For now, log a severe warning and return only the (potentially truncated) last system message.
            logger.error(f"System messages ({tokens_for_system_messages} tokens) exceed total token limit ({token_limit}). "
                         f"Attempting to use only the last system message.")
            if system_messages:
                last_system_message = system_messages[-1]
                truncated_system_content = await self._truncate_message_content(
                    last_system_message.get('content',''), 
                    token_limit - (await self.count_tokens_in_messages([{"role":"system", "content":""}], model)), # Approx overhead
                    model
                )
                if truncated_system_content:
                    final_system_msg = [{"role": "system", "content": truncated_system_content}]
                    if (await self.count_tokens_in_messages(final_system_msg, model)) <= token_limit:
                        return final_system_msg
            return [] # Cannot form a valid context

        optimized_dialogue_messages: List[Dict[str, str]] = []
        current_dialogue_tokens = 0
        
        # Iterate through dialogue messages from newest to oldest
        for msg_to_add in reversed(dialogue_messages):
            tokens_for_this_msg_full = await self.count_tokens_in_messages([msg_to_add], model) # Includes overhead
            
            if current_dialogue_tokens + tokens_for_this_msg_full <= remaining_token_limit_for_dialogue:
                optimized_dialogue_messages.insert(0, msg_to_add) # Add to the beginning to maintain order
                current_dialogue_tokens += tokens_for_this_msg_full
            else:
                # Message doesn't fit. Try to truncate its content.
                # Calculate overhead for this message (role + structure, without content)
                empty_msg_with_role = {"role": msg_to_add.get('role', 'user'), "content": ""}
                overhead_for_one_msg = await self.count_tokens_in_messages([empty_msg_with_role], model)
                
                # Max tokens allowed for the content of this message
                content_token_limit = remaining_token_limit_for_dialogue - current_dialogue_tokens - overhead_for_one_msg
                
                if content_token_limit > 10: # Arbitrary threshold for meaningful truncation
                    truncated_content = await self._truncate_message_content(
                        msg_to_add.get('content', ''),
                        content_token_limit,
                        model
                    )
                    if truncated_content:
                        truncated_msg_obj = {**msg_to_add, 'content': truncated_content}
                        # Check if the truncated message now fits
                        tokens_for_truncated_msg_full = await self.count_tokens_in_messages([truncated_msg_obj], model)
                        if current_dialogue_tokens + tokens_for_truncated_msg_full <= remaining_token_limit_for_dialogue:
                            optimized_dialogue_messages.insert(0, truncated_msg_obj)
                            current_dialogue_tokens += tokens_for_truncated_msg_full
                            logger.info(f"Message truncated and added. Tokens for msg: {tokens_for_truncated_msg_full}. Total dialogue tokens: {current_dialogue_tokens}")
                        else:
                            logger.info(f"Truncated message still too large ({tokens_for_truncated_msg_full} tokens). Skipping.")
                    else:
                        logger.info(f"Failed to truncate message content for user: {msg_to_add.get('role')}, content start: '{msg_to_add.get('content', '')[:30]}...'. Skipping.")
                else:
                    logger.info(f"Not enough token budget ({content_token_limit}) to include even a truncated part of message. Skipping.")
                
                # Since we couldn't fit the current message (even truncated), stop adding older messages.
                break 
        
        final_optimized_list = system_messages + optimized_dialogue_messages
        final_tokens_count = await self.count_tokens_in_messages(final_optimized_list, model) # Recalculate final total
        
        logger.info(f"Context optimized: {len(final_optimized_list)} messages, {final_tokens_count} tokens (Limit: {token_limit}).")
        return final_optimized_list
